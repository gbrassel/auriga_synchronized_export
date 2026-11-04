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

        def handle_request(request):
            if "/api/" in request.url:
                auth = request.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    token_box.append(auth)

        page.on("request", handle_request)
        
        log("Navigation vers Auriga...")
        page.goto(PLANNING_URL)

        # Clic sur le bouton SSO ISAE-SUPAERO
        try:
            log("Recherche du bouton SSO...")
            page.get_by_text("SSO ISAE-SUPAERO").click(timeout=10000)
            log("Bouton cliqué. En attente du portail ISAE...")
            # On attend un peu que la redirection vers le vrai CAS se fasse
            page.wait_for_load_state("networkidle")
        except Exception:
            log("Bouton SSO non trouvé, on tente de remplir directement...")

        # Remplissage des identifiants sur le portail CAS de l'ISAE-SUPAERO
        try:
            log("Remplissage des identifiants SSO...")
            page.locator("#username").fill(user)
            page.locator("#password").fill(password)
            # Appuyer sur Entrée est plus fiable que de chercher le bouton submit
            page.keyboard.press("Enter")
            
            # Attendre spécifiquement que le SSO nous redirige vers Auriga (15s max)
            page.wait_for_url("**/auriga.isae-supaero.fr/**", timeout=15000)
            log("Redirection réussie.")
        except Exception as e:
            log(f"Avertissement lors de la connexion SSO : {e}")

        log("Attente de l'interception du token OIDC (30s max)...")
        for i in range(30):
            if token_box:
                break
            page.wait_for_timeout(1000)
            
            # Nudge : Si on est sur l'accueil au lieu du planning, on force la route
            if i == 10 and not token_box:
                log("Forçage de l'URL vers la vue planning...")
                page.goto(PLANNING_URL)

        if not token_box:
            # Capture d'écran cruciale pour le débogage
            page.screenshot(path="error.png")
            browser.close()
            raise RuntimeError("Impossible de récupérer le token OIDC. Consultez error.png pour comprendre le blocage.")

        browser.close()
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
    def txt(multi, default=""): 
        if isinstance(multi, dict):
            return multi.get("fr") or multi.get("en") or default
        return default
        
    def parse_dt(s): 
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    
    # 1. Ressources (Salles) - Correction : on accepte TOUTES les salles, y compris les "COUPLEES"
    rooms = []
    for r_link in iv.get("interventionResources") or []:
        res = r_link.get("resource") or {}
        if res.get("isRoom"):
            caption = txt(res.get("caption")) or res.get("code") or ""
            if caption:
                rooms.append(caption)
                
    # 2. Intervenants
    instructors = []
    for i_link in iv.get("interventionInstructors") or []:
        pers = i_link.get("person") or {}
        name = " ".join(filter(None, [pers.get("currentFirstName"), pers.get("currentLastName")])).title()
        if name:
            instructors.append(name)
            
    # 3. Groupes et Populations
    groups = []
    for p_link in iv.get("interventionPopulations") or []:
        pop = p_link.get("population") or {}
        label = txt(pop.get("caption")) or pop.get("code") or ""
        if label:
            groups.append(label)
            
    # 4. Modules Pédagogiques (Le véritable nom du cours !)
    modules = []
    for pu_link in iv.get("interventionPedagogicalUnits") or []:
        pu = pu_link.get("pedagogicalUnit") or {}
        mod_name = txt(pu.get("caption"))
        if mod_name:
            modules.append(mod_name)
            
    # 5. Extraction des métadonnées de base
    at = iv.get("activityType") or {}
    type_label = txt(at.get("caption"), "Activité")
    field_label = txt((iv.get("field") or {}).get("caption"))
    description_raw = (iv.get("description") or "").strip()
    status = txt((iv.get("interventionStatus") or {}).get("caption"))
    
    # Détection des examens (via le booléen d'Auriga ou le type d'activité)
    is_exam = bool(iv.get("isExam")) or ("exam" in type_label.lower())
    
    # 6. Logique de construction du Titre Explicite
    # Priorité : Module Pédagogique > Field > Première ligne de la description
    main_subject = ""
    if modules:
        main_subject = modules[0]
    elif field_label:
        main_subject = field_label
    elif description_raw:
        main_subject = description_raw.split('\n')[0]
        
    if main_subject:
        title = f"{main_subject} ({type_label})"
    else:
        title = type_label # En tout dernier recours (très rare)

    # 7. Ajout des alertes critiques dans le titre
    if is_exam:
        title = f"[EXAMEN] {title}"
    if status and "annulé" in status.lower():
        title = f"[ANNULÉ] {title}"
        
    # 8. Nettoyage de la description pour éviter les doublons
    final_desc = description_raw
    if main_subject == description_raw.split('\n')[0]:
        # Si la première ligne de la description a servi de titre, on l'enlève du corps
        final_desc = "\n".join(description_raw.split('\n')[1:]).strip()
        
    return {
        "id": iv["id"],
        "start": parse_dt(iv["startDateTime"]),
        "end": parse_dt(iv["endDateTime"]),
        "title": title,
        "description": final_desc,
        "rooms": sorted(set(rooms)),
        "instructors": sorted(set(instructors)),
        "groups": sorted(set(groups)),
        "status": status,
        "is_exam": is_exam
    }

def build_ics(events: list[dict]) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//auriga-export//ISAE-SUPAERO//FR", "CALSCALE:GREGORIAN", "X-WR-CALNAME:Planning ISAE", "X-WR-TIMEZONE:Europe/Paris"]
    
    for ev in events:
        desc_parts = []
        if ev["description"]:
            desc_parts.append(ev["description"])
        if ev["instructors"]:
            desc_parts.append("Intervenant(s) : " + ", ".join(ev["instructors"]))
        if ev["groups"]:
            desc_parts.append("Groupe(s) : " + ", ".join(ev["groups"]))
        if ev["status"]:
            desc_parts.append(f"Statut : {ev['status']}")
            # Si le statut indique une annulation, on peut aussi l'ajouter au titre
            if "annulé" in ev["status"].lower():
                ev["title"] = "[ANNULÉ] " + ev["title"]
        
        desc_text = "\\n\\n".join(desc_parts).replace(",", r"\,").replace(";", r"\;")
        title_text = ev["title"].replace(",", r"\,").replace(";", r"\;")
        loc_text = ", ".join(ev["rooms"]).replace(",", r"\,").replace(";", r"\;")

        lines += [
            "BEGIN:VEVENT",
            f"UID:auriga-{ev['id']}@isae-supaero.fr",
            f"DTSTAMP:{stamp}",
            "DTSTART:" + ev["start"].strftime("%Y%m%dT%H%M%SZ"),
            "DTEND:" + ev["end"].strftime("%Y%m%dT%H%M%SZ"),
            "SUMMARY:" + title_text,
            "LOCATION:" + loc_text,
            "DESCRIPTION:" + desc_text,
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