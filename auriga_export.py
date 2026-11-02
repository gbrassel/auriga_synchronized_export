import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

BASE = "https://auriga.isae-supaero.fr"
API = BASE + "/api"
PLANNING_URL = BASE + "/#/mainContent/menuEntry/227/planning"

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

# --------------------------------------------------------------------------
# 1. Authentification SSO et interception du Token avec Playwright
# --------------------------------------------------------------------------
def grab_token() -> str:
    user = os.environ.get("ISAE_USER")
    password = os.environ.get("ISAE_PASS")
    
    if not user or not password:
        raise ValueError("Variables d'environnement ISAE_USER ou ISAE_PASS manquantes.")

    token_box = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Interception réseau pour sniffer le Bearer token
        def handle_request(request):
            if "/api/" in request.url:
                auth = request.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    token_box.append(auth)

        page.on("request", handle_request)
        
        log("Navigation vers Auriga...")
        page.goto(PLANNING_URL)

        # Remplissage du SSO ISAE (Sélecteurs CAS standards)
        try:
            log("Remplissage des identifiants SSO...")
            page.locator("#username").fill(user)
            page.locator("#password").fill(password)
            page.locator("input[name='submit'], button[name='submit'], input[type='submit']").first.click()
        except PlaywrightTimeout:
            log("Formulaire SSO non trouvé. Peut-être déjà connecté ou l'interface a changé.")

        # Attente du chargement de l'app Angular et de la requête API
        log("Attente de l'interception du token OIDC...")
        for _ in range(15):
            if token_box:
                break
            page.wait_for_timeout(1000)

        browser.close()

        if not token_box:
            raise RuntimeError("Impossible de récupérer le token OIDC (Délai dépassé ou mauvais identifiants).")

        return token_box[0]

# --------------------------------------------------------------------------
# 2. Publication sur le Gist GitHub
# --------------------------------------------------------------------------
def publish_to_gist(ics_content: str):
    gist_id = os.environ.get("GIST_ID")
    github_token = os.environ.get("GIST_TOKEN")
    
    if not gist_id or not github_token:
        log("Variables GIST_ID ou GIST_TOKEN manquantes. Publication GitHub ignorée.")
        return

    log(f"Mise à jour du Gist {gist_id}...")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        method="PATCH",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        },
        data=json.dumps({"files": {"auriga.ics": {"content": ics_content}}}).encode("utf-8")
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                log("✅ Gist mis à jour avec succès. Outlook va se synchroniser automatiquement.")
    except Exception as e:
        log(f"❌ Erreur lors de la mise à jour du Gist : {e}")

# --------------------------------------------------------------------------
# 3. Récupération et conversion (Code d'origine conservé)
# --------------------------------------------------------------------------
def fetch_window(token: str, start: str, end: str, timeout: float = 60.0) -> dict:
    qs = urllib.parse.urlencode([("days", d) for d in range(1, 8)] + [("startDate", start), ("endDate", end)])
    req = urllib.request.Request(
        f"{API}/plannings/me?{qs}",
        headers={"Authorization": token, "x-scope": "frontend", "Accept": "application/json", "accept-language": "fr"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def daterange_chunks(start: dt.date, end: dt.date, days: int):
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=days - 1), end)
        yield cur.isoformat(), stop.isoformat()
        cur = stop + dt.timedelta(days=1)

def normalise(iv: dict) -> dict:
    def txt(multi, default=""): return multi.get("fr") or multi.get("en") or default if isinstance(multi, dict) else default
    def parse_dt(s): return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    
    rooms = [txt(l.get("resource", {}).get("caption")) or l.get("resource", {}).get("code", "") 
             for l in iv.get("interventionResources") or [] if l.get("resource", {}).get("isRoom") and "COUPLEES" not in l.get("resource", {}).get("code", "").upper()]
    instructors = [" ".join(filter(None, [l.get("person", {}).get("currentFirstName"), l.get("person", {}).get("currentLastName")])).title() 
                   for l in iv.get("interventionInstructors") or []]
    
    at = iv.get("activityType") or {}
    return {
        "id": iv["id"],
        "start": parse_dt(iv["startDateTime"]),
        "end": parse_dt(iv["endDateTime"]),
        "title": re.sub(r"\s+", " ", (iv.get("description") or "").strip()) or txt(at.get("caption"), "Cours"),
        "type_label": txt(at.get("caption")),
        "rooms": sorted(set(rooms)),
        "instructors": sorted(set(instructors)),
    }

def build_ics(events: list[dict]) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//auriga-export//ISAE-SUPAERO//FR", "CALSCALE:GREGORIAN", "X-WR-CALNAME:ISAE", "X-WR-TIMEZONE:Europe/Paris"]
    
    for ev in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:auriga-{ev['id']}@isae-supaero.fr",
            f"DTSTAMP:{stamp}",
            "DTSTART:" + ev["start"].strftime("%Y%m%dT%H%M%SZ"),
            "DTEND:" + ev["end"].strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + ev["title"].replace(",", r"\,").replace(";", r"\;"),
            "LOCATION:" + ", ".join(ev["rooms"]).replace(",", r"\,"),
            "DESCRIPTION:Intervenants: " + ", ".join(ev["instructors"]).replace(",", r"\,"),
            "END:VEVENT"
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

def main():
    today = dt.date.today()
    start = dt.date(today.year if today.month >= 8 else today.year - 1, 8, 1)
    end = dt.date(start.year + 1, 8, 31)

    log("Démarrage de la récupération Auriga...")
    token = grab_token()
    log("Token obtenu avec succès.")

    events = []
    for s, e in daterange_chunks(start, end, 7):
        data = fetch_window(token, s, e)
        events.extend([normalise(iv) for iv in data.get("interventions", [])])
        time.sleep(0.1)

    # Dé-doublonnage par ID (car les fenêtres glissantes peuvent se chevaucher)
    events = list({ev["id"]: ev for ev in events}.values())
    
    if not events:
        log("Aucun événement trouvé.")
        return

    ics = build_ics(events)
    with open("auriga.ics", "w", encoding="utf-8") as f:
        f.write(ics)
    
    log(f"{len(events)} événements générés.")
    publish_to_gist(ics)

if __name__ == "__main__":
    main()