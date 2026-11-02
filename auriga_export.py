#!/usr/bin/env python3
"""
Auriga -> iCalendar exporter (ISAE-SUPAERO).

Pulls your personal planning from https://auriga.isae-supaero.fr and writes a
.ics file you can import into Google Calendar (or any calendar app).

How it works
------------
Auriga is an Angular SPA whose planning grid is fed by:

    GET /api/plannings/me?days=1..7&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD

That endpoint is protected by a short-lived OIDC Bearer token which the SPA
keeps in memory (not in cookies or localStorage). So this script:

  1. launches Chromium with the DevTools protocol enabled,
  2. waits for you to log in through the normal ISAE SSO page,
  3. sniffs the Authorization header off one of the app's own API calls,
  4. then talks to the API directly from Python and writes the .ics.

Only the Python standard library is required (the DevTools client, including a
minimal WebSocket implementation, is built in).

Usage
-----
    python3 auriga_export.py                       # current academic year -> auriga.ics
    python3 auriga_export.py --start 2026-09-01 --end 2027-02-28
    python3 auriga_export.py -o ~/planning.ics --json raw.json
    python3 auriga_export.py --list                # print the schedule, no file

Then in Google Calendar: Settings -> Import & export -> Import, pick the .ics.
Event UIDs are stable, so re-importing an updated file updates the existing
events instead of duplicating them.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://auriga.isae-supaero.fr"
API = BASE + "/api"
PLANNING_URL = BASE + "/#/mainContent/menuEntry/227/planning"
APP_HOST = "auriga.isae-supaero.fr"
DEFAULT_PORT = 9222


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Minimal WebSocket client (stdlib only) -- enough for the DevTools protocol.
# --------------------------------------------------------------------------
class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0):
        p = urllib.parse.urlparse(url)
        if p.scheme != "ws":
            raise ValueError("only ws:// supported, got %r" % url)
        host = p.hostname or "localhost"
        port = p.port or 80
        path = p.path + (("?" + p.query) if p.query else "")

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed during WebSocket handshake")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise ConnectionError("WebSocket upgrade refused: %s" % buf.split(b"\r\n")[0])
        self._rest = buf.split(b"\r\n\r\n", 1)[1]

    # -- raw byte helpers ---------------------------------------------------
    def _read(self, n: int) -> bytes:
        out, self._rest = self._rest[:n], self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(max(4096, n - len(out)))
            if not chunk:
                raise ConnectionError("WebSocket closed by peer")
            need = n - len(out)
            out += chunk[:need]
            self._rest = chunk[need:]
        return out

    def _read_frame(self):
        b1, b2 = self._read(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        masked, length = bool(b2 & 0x80), b2 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read(8))
        mask = self._read(4) if masked else None
        payload = self._read(length) if length else b""
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return fin, opcode, payload

    def send(self, text: str) -> None:
        data = text.encode()
        header = bytearray([0x81])  # FIN + text
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def recv(self) -> str:
        """Return the next complete text message (handles fragments + pings)."""
        chunks: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:                      # close
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:                      # ping -> pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:                      # pong
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class DevTools:
    """Just enough Chrome DevTools Protocol to reload a tab and read headers."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws = WebSocket(ws_url, timeout=timeout)
        self._id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.sock.settimeout(max(0.5, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(f"no reply to {method}")

    def drain(self, seconds: float) -> None:
        """Collect incoming events for a while."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.ws.sock.settimeout(max(0.2, deadline - time.time()))
            try:
                msg = json.loads(self.ws.recv())
            except (socket.timeout, TimeoutError):
                return
            except ConnectionError:
                return
            if "method" in msg:
                self.events.append(msg)

    def close(self) -> None:
        self.ws.close()


# --------------------------------------------------------------------------
# Browser handling: launch Chromium, wait for login, steal the bearer token.
# --------------------------------------------------------------------------
def default_profile() -> str:
    """Per-OS place to keep the browser profile, so the login sticks."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "auriga-export", "chrome-profile")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/auriga-export/chrome-profile")
    return os.path.expanduser("~/.config/auriga-export/chrome-profile")


def find_chromium() -> str:
    """Locate a Chromium-family browser on Windows, macOS or Linux."""
    if os.name == "nt":
        names = ["chrome", "msedge", "brave", "chromium"]
    elif sys.platform == "darwin":
        names = ["chromium", "google-chrome", "brave"]
    else:
        names = ["chromium", "chromium-browser", "google-chrome-stable",
                 "google-chrome", "chrome", "brave", "microsoft-edge"]
    for name in names:
        path = shutil.which(name)
        if path:
            return path

    candidates: list[str] = []
    if os.name == "nt":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if not root:
                continue
            candidates += [
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(root, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                os.path.join(root, "Chromium", "Application", "chrome.exe"),
            ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    raise SystemExit(
        "Aucun navigateur Chrome/Chromium/Edge trouve.\n"
        "Installez-en un, ou indiquez le chemin : --browser \"C:\\...\\chrome.exe\""
    )


# The DevTools port is local: never send it through a system/corporate proxy
# (a common failure mode on Windows). The Auriga call itself still uses the
# normal proxy settings.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def devtools_json(port: int, path: str = "/json") -> list | dict:
    with _LOCAL.open(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.load(r)


def port_alive(port: int) -> bool:
    try:
        devtools_json(port, "/json/version")
        return True
    except Exception:
        return False


def launch_browser(browser: str, port: int, profile: str) -> subprocess.Popen | None:
    if port_alive(port):
        log(f"Navigateur deja ouvert sur le port {port}, reutilise.")
        return None
    os.makedirs(profile, exist_ok=True)
    log(f"Lancement de {os.path.basename(browser)} ...")
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [browser,
         f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check",
         "--disable-features=Translate",
         PLANNING_URL],
        **kwargs,
    )
    for _ in range(80):
        if port_alive(port):
            return proc
        if proc.poll() is not None:          # exited at once
            raise SystemExit(
                "Le navigateur s'est ferme immediatement.\n"
                f"Le profil est sans doute deja utilise : {profile}\n"
                "Fermez cette fenetre de navigateur, ou relancez avec "
                "--profile <autre_dossier>."
            )
        time.sleep(0.5)
    raise SystemExit(
        f"Le navigateur n'a pas ouvert le port {port} a temps.\n"
        "Reessayez avec --port <autre_port>."
    )


def list_pages(port: int) -> list[dict]:
    try:
        targets = devtools_json(port)
    except Exception:
        return []
    return [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]


def pick_target(port: int) -> dict:
    """The Auriga tab if we can see it, otherwise whatever tab is open.

    During SSO the tab sits on the identity provider's domain, so we must not
    insist on the Auriga host here.
    """
    pages = list_pages(port)
    if not pages:
        raise ConnectionError("aucun onglet disponible")
    for t in pages:
        if APP_HOST in (t.get("url") or ""):
            return t
    return pages[0]


def _token_from(events: list[dict]) -> str | None:
    for msg in events:
        if msg.get("method") != "Network.requestWillBeSent":
            continue
        req = msg["params"].get("request") or {}
        if "/api/" not in (req.get("url") or ""):
            continue
        for key, value in (req.get("headers") or {}).items():
            if key.lower() == "authorization" and (value or "").strip():
                return value
    return None


# Re-enters the planning route to make the SPA refetch. A hash change does not
# reload the document, so it can never wipe a half-filled login form.
NUDGE_JS = """(function () {
  var target = '#/mainContent/menuEntry/227/planning';
  if (location.hash === target) {
    location.hash = '#/mainContent/welcome';
    setTimeout(function () { location.hash = target; }, 250);
  } else {
    location.hash = target;
  }
})()"""


def grab_token(port: int, wait_minutes: float) -> str:
    """Watch the app's own API traffic until one request carries a bearer token.

    Deliberately passive: the tab is never reloaded while you might be typing on
    the SSO form. Once the app is loaded but idle, it is only nudged through a
    hash route change, which does not reload the page.
    """
    started = time.time()
    deadline = started + wait_minutes * 60
    dev: DevTools | None = None
    connected_at = 0.0
    last_nudge = 0.0
    prompted = False

    try:
        while time.time() < deadline:
            try:
                if dev is None:
                    dev = DevTools(pick_target(port)["webSocketDebuggerUrl"], timeout=15)
                    dev.call("Network.enable", timeout=15)
                    connected_at = time.time()
                    last_nudge = 0.0

                dev.drain(3.0)
                token = _token_from(dev.events)
                if token:
                    return token
                dev.events.clear()

                # Only nudge a loaded app; while the tab is on the SSO domain,
                # stay completely out of the way.
                url = pick_target(port).get("url") or ""
                now = time.time()
                if APP_HOST in url:
                    idle = now - max(connected_at, last_nudge)
                    if idle > (3.0 if not last_nudge else 20.0):
                        dev.call("Runtime.evaluate", {"expression": NUDGE_JS}, timeout=10)
                        last_nudge = now

            except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError, KeyError):
                # tab navigated away (SSO redirect) or closed -- reconnect
                if dev is not None:
                    dev.close()
                dev = None
                time.sleep(1.5)

            if not prompted and time.time() - started > 15:
                log("")
                log("  >> Connectez-vous a Auriga dans la fenetre du navigateur.")
                log("     L'export demarre tout seul ensuite. (Ctrl-C pour annuler)")
                log("")
                prompted = True

        raise SystemExit("Delai depasse en attendant la connexion.")
    finally:
        if dev is not None:
            dev.close()


def close_browser(port: int, proc: subprocess.Popen | None) -> None:
    """Ask the browser to quit. On Windows Chrome re-spawns itself, so killing
    the process we launched is not enough -- Browser.close is the reliable way."""
    try:
        endpoint = devtools_json(port, "/json/version")["webSocketDebuggerUrl"]
        ws = WebSocket(endpoint, timeout=5)
        ws.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
        time.sleep(0.5)
        ws.close()
        return
    except Exception:
        pass
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass


def token_expiry(token: str) -> dt.datetime | None:
    try:
        payload = token.split(" ")[-1].split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return dt.datetime.fromtimestamp(exp, dt.timezone.utc)
    except Exception:
        return None


# --------------------------------------------------------------------------
# The API itself
# --------------------------------------------------------------------------
def fetch_window(token: str, start: str, end: str, timeout: float = 60.0) -> dict:
    qs = urllib.parse.urlencode([("days", d) for d in range(1, 8)]
                                + [("startDate", start), ("endDate", end)])
    req = urllib.request.Request(
        f"{API}/plannings/me?{qs}",
        headers={"Authorization": token, "x-scope": "frontend",
                 "Accept": "application/json", "accept-language": "fr"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def daterange_chunks(start: dt.date, end: dt.date, days: int):
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=days - 1), end)
        yield cur.isoformat(), stop.isoformat()
        cur = stop + dt.timedelta(days=1)


def fetch_all(get_token, start: dt.date, end: dt.date, chunk_days: int) -> list[dict]:
    """Fetch the range in chunks, de-duplicating by intervention id."""
    token = get_token()
    by_id: dict[int, dict] = {}
    windows = list(daterange_chunks(start, end, chunk_days))
    for i, (s, e) in enumerate(windows, 1):
        for attempt in (1, 2):
            try:
                data = fetch_window(token, s, e)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and attempt == 1:
                    log("  token expired, refreshing...")
                    token = get_token(force=True)
                    continue
                log(f"  {s}..{e}: HTTP {exc.code}, skipped")
                data = {"interventions": []}
                break
            except Exception as exc:                       # transient network issue
                if attempt == 1:
                    time.sleep(2)
                    continue
                log(f"  {s}..{e}: {exc}, skipped")
                data = {"interventions": []}
                break
        for iv in data.get("interventions", []):
            by_id[iv["id"]] = iv
        print(f"\r  {i}/{len(windows)} windows  ({len(by_id)} events)",
              end="", file=sys.stderr, flush=True)
        time.sleep(0.15)
    print(file=sys.stderr)
    return sorted(by_id.values(), key=lambda e: (e["startDateTime"], e["id"]))


# --------------------------------------------------------------------------
# Normalising an intervention into calendar-shaped fields
# --------------------------------------------------------------------------
HOLIDAY_RE = re.compile(r"vacances|f[ée]ri[ée]", re.I)


def txt(multi, default: str = "") -> str:
    """Auriga stores captions as {'fr': ..., 'en': ...}."""
    if not isinstance(multi, dict):
        return default
    for k in ("fr", "en"):
        if multi.get(k):
            return str(multi[k])
    return default


def parse_dt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def rooms_of(iv: dict) -> list[str]:
    out = []
    for link in iv.get("interventionResources") or []:
        res = link.get("resource") or {}
        if not res.get("isRoom"):
            continue
        code = res.get("code") or ""
        if "COUPLEES" in code.upper():        # "coupled rooms" duplicate the real ones
            continue
        out.append(txt(res.get("caption")) or code)
    return sorted(set(out))


def instructors_of(iv: dict) -> list[str]:
    out = []
    for link in iv.get("interventionInstructors") or []:
        p = link.get("person") or {}
        name = " ".join(x for x in (p.get("currentFirstName"), p.get("currentLastName")) if x)
        if name:
            out.append(name.title())
    return sorted(set(out))


def populations_of(iv: dict) -> list[str]:
    """The filiere / parcours / groups an intervention is scheduled for."""
    out = []
    for link in iv.get("interventionPopulations") or []:
        pop = link.get("population") or {}
        label = txt(pop.get("caption")) or pop.get("code") or ""
        if label:
            out.append(label)
    return sorted(set(out))


def normalise(iv: dict) -> dict:
    at = iv.get("activityType") or {}
    raw_desc = (iv.get("description") or "").strip()
    return {
        "id": iv["id"],
        "start": parse_dt(iv["startDateTime"]),
        "end": parse_dt(iv["endDateTime"]),
        "title": re.sub(r"\s+", " ", raw_desc) or txt(at.get("caption"), "Cours"),
        "full_desc": raw_desc,
        "type_code": at.get("code") or "",
        "type_label": txt(at.get("caption")),
        "rooms": rooms_of(iv),
        "instructors": instructors_of(iv),
        "groups": populations_of(iv),
        "field": txt((iv.get("field") or {}).get("caption")),
        "is_exam": bool(iv.get("isExam")),
        "status": txt((iv.get("interventionStatus") or {}).get("caption")),
    }


def is_holiday_marker(ev: dict) -> bool:
    return ev["type_code"] == "REU" and bool(HOLIDAY_RE.search(ev["title"]))


# --------------------------------------------------------------------------
# iCalendar writing
# --------------------------------------------------------------------------
def esc(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", r"\;")
                 .replace(",", r"\,").replace("\r\n", r"\n")
                 .replace("\n", r"\n").replace("\r", r"\n"))


def fold(line: str) -> str:
    """RFC 5545 line folding: <=75 octets per line, continuations start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74          # continuation lines carry a leading space
        if len(cur) + len(b) > limit:
            out.append(cur)
            cur = b""
        cur += b
    out.append(cur)
    return ("\r\n ".join(p.decode("utf-8") for p in out))


def build_ics(events: list[dict], calname: str, allday_holidays: bool,
              prefix_type: bool, reminder: int | None, all_groups: bool = False) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//auriga-export//ISAE-SUPAERO//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(calname)}",
        "X-WR-TIMEZONE:Europe/Paris",
    ]

    for ev in events:
        summary = ev["title"]
        if prefix_type and ev["type_code"] and not is_holiday_marker(ev):
            summary = f"[{ev['type_code']}] {summary}"
        if ev["is_exam"]:
            summary = "EXAM - " + summary

        desc_parts = []
        if ev["full_desc"] and ev["full_desc"] != ev["title"]:
            desc_parts.append(ev["full_desc"])
        if ev["type_label"]:
            desc_parts.append(f"Type : {ev['type_label']}")
        if ev["instructors"]:
            desc_parts.append("Intervenant(s) : " + ", ".join(ev["instructors"]))
        if ev["rooms"]:
            desc_parts.append("Salle(s) : " + ", ".join(ev["rooms"]))
        if ev["field"]:
            desc_parts.append(f"Domaine : {ev['field']}")
        if ev["groups"]:
            if all_groups or len(ev["groups"]) <= 3:
                desc_parts.append("Groupe(s) : " + ", ".join(ev["groups"]))
            else:
                desc_parts.append("Groupe(s) : " + ", ".join(ev["groups"][:3])
                                  + f" (+{len(ev['groups']) - 3} autres)")
        if ev["status"]:
            desc_parts.append(f"Statut : {ev['status']}")
        desc_parts.append(f"Auriga #{ev['id']}")

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:auriga-{ev['id']}@isae-supaero.fr")
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"LAST-MODIFIED:{stamp}")

        if allday_holidays and is_holiday_marker(ev):
            d0 = ev["start"].astimezone(dt.timezone.utc).date()
            d1 = d0 + dt.timedelta(days=1)
            lines.append(f"DTSTART;VALUE=DATE:{d0.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{d1.strftime('%Y%m%d')}")
            lines.append("TRANSP:TRANSPARENT")
        else:
            lines.append("DTSTART:" + ev["start"].strftime("%Y%m%dT%H%M%SZ"))
            lines.append("DTEND:" + ev["end"].strftime("%Y%m%dT%H%M%SZ"))
            lines.append("TRANSP:OPAQUE")

        lines.append("SUMMARY:" + esc(summary))
        if ev["rooms"]:
            lines.append("LOCATION:" + esc(", ".join(ev["rooms"])))
        lines.append("DESCRIPTION:" + esc("\n".join(desc_parts)))
        if ev["type_label"]:
            lines.append("CATEGORIES:" + esc(ev["type_label"]))
        lines.append("URL:" + PLANNING_URL)
        lines.append("STATUS:CONFIRMED")
        if reminder is not None and not (allday_holidays and is_holiday_marker(ev)):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"TRIGGER:-PT{reminder}M",
                      "DESCRIPTION:" + esc(summary), "END:VALARM"]
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# --------------------------------------------------------------------------
def academic_year_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    """ISAE academic year runs late August -> late August."""
    year = today.year if today.month >= 8 else today.year - 1
    return dt.date(year, 8, 1), dt.date(year + 1, 8, 31)


def print_table(events: list[dict]) -> None:
    for ev in events:
        local_start = ev["start"] + dt.timedelta(hours=2)   # display hint only
        rooms = ", ".join(ev["rooms"])[:34]
        who = ", ".join(ev["instructors"])[:24]
        print(f"{ev['start']:%Y-%m-%d} {ev['start']:%H:%M}-{ev['end']:%H:%M}Z "
              f"[{ev['type_code']:<7}] {ev['title'][:52]:<52} {rooms:<34} {who}")


def main() -> int:
    today = dt.date.today()
    d0, d1 = academic_year_bounds(today)

    ap = argparse.ArgumentParser(
        description="Export your ISAE-SUPAERO Auriga planning to an .ics file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Import the result in Google Calendar via Settings > Import & export.",
    )
    ap.add_argument("--start", default=d0.isoformat(), help=f"first day (default {d0})")
    ap.add_argument("--end", default=d1.isoformat(), help=f"last day (default {d1})")
    ap.add_argument("-o", "--output", default="auriga.ics", help="output .ics (default auriga.ics)")
    ap.add_argument("--json", metavar="FILE", help="also dump the raw API payload here")
    ap.add_argument("--list", action="store_true", help="print the schedule and skip writing the .ics")
    ap.add_argument("--calname", default="Auriga - ISAE-SUPAERO", help="calendar display name")
    ap.add_argument("--chunk-days", type=int, default=7,
                    help="days per API request (default 7, same as the web UI)")
    ap.add_argument("--prefix-type", action="store_true",
                    help="prefix event titles with the activity code, e.g. '[PC] Anglais'")
    ap.add_argument("--all-groups", action="store_true",
                    help="list every filiere/parcours on an event instead of the first 3")
    ap.add_argument("--reminder", type=int, metavar="MIN",
                    help="add a popup reminder N minutes before each event")
    ap.add_argument("--no-allday-holidays", dest="allday_holidays", action="store_false",
                    help="keep VACANCES/FERIE markers as timed events instead of all-day")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="DevTools port")
    ap.add_argument("--profile", default=default_profile(),
                    help="browser profile directory (login is remembered there)")
    ap.add_argument("--browser", help="path to Chromium/Chrome")
    ap.add_argument("--wait-minutes", type=float, default=15.0, help="how long to wait for login")
    ap.add_argument("--keep-browser", action="store_true", help="leave the browser open when done")
    args = ap.parse_args()

    try:
        start = dt.date.fromisoformat(args.start)
        end = dt.date.fromisoformat(args.end)
    except ValueError as exc:
        return log(f"Bad date: {exc}") or 2
    if end < start:
        return log("--end is before --start") or 2

    browser = args.browser or find_chromium()
    proc = launch_browser(browser, args.port, args.profile)

    cached: dict[str, str] = {}

    def get_token(force: bool = False) -> str:
        if force or "t" not in cached:
            cached["t"] = grab_token(args.port, args.wait_minutes)
            exp = token_expiry(cached["t"])
            log(f"Session OK{f' (token valid until {exp:%H:%M:%S} UTC)' if exp else ''}.")
        return cached["t"]

    try:
        log(f"Fetching {start} -> {end} ...")
        raw = fetch_all(get_token, start, end, max(1, args.chunk_days))
    finally:
        if proc and not args.keep_browser:
            close_browser(args.port, proc)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=1)
        log(f"Raw payload -> {args.json}")

    events = [normalise(iv) for iv in raw]
    if not events:
        log("No events in that range. Nothing written.")
        return 1

    if args.list:
        print_table(events)
        return 0

    ics = build_ics(events, args.calname, args.allday_holidays,
                    args.prefix_type, args.reminder, args.all_groups)
    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        fh.write(ics)

    span = f"{events[0]['start']:%Y-%m-%d} .. {events[-1]['start']:%Y-%m-%d}"
    log(f"Wrote {len(events)} events ({span}) -> {args.output}")
    log("Google Calendar: Settings > Import & export > Import.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\nAborted.")
        sys.exit(130)
