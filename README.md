# Auriga → Google Agenda

Exporte ton planning Auriga (ISAE-SUPAERO) en fichier `.ics`.

## Dépendances

**1. Python ≥ 3.7** (testé sur 3.14). Rien à installer avec `pip` : le script
n'utilise que la bibliothèque standard — `argparse`, `base64`, `datetime`, `json`,
`os`, `re`, `shutil`, `socket`, `struct`, `subprocess`, `sys`, `time`, `urllib`.
Le client WebSocket/DevTools est écrit à la main dans le script, d'où l'absence de
`selenium`, `playwright` ou `puppeteer`.

**2. Un navigateur de la famille Chromium** — Chrome, Chromium, Edge ou Brave.
Sert uniquement à la connexion SSO et à la récupération du jeton.
**Firefox ne convient pas** : il n'expose pas le protocole DevTools utilisé ici.

| Système | Installation |
|---|---|
| Windows | rien à faire, Edge est déjà présent (Chrome convient aussi) |
| Manjaro / Arch | `sudo pacman -S chromium` |
| Debian / Ubuntu | `sudo apt install chromium` |
| macOS | `brew install --cask chromium` (ou Chrome déjà installé) |

Détection automatique via le `PATH` puis les emplacements d'installation habituels.
Sinon : `--browser "C:\Program Files\Google\Chrome\Application\chrome.exe"`.

**3. Un accès réseau** à `auriga.isae-supaero.fr` et au SSO ISAE (VPN si tu es hors campus).

Le port DevTools local contourne volontairement le proxy système ; le reste des
requêtes suit la configuration proxy normale.

## Utilisation

```bash
python3 auriga_export.py        # Linux / macOS
python auriga_export.py         # Windows
```

Le navigateur s'ouvre, tu te connectes **une seule fois**, et `auriga.ics` est écrit.

Le profil du navigateur est conservé (`~/.config/auriga-export` sous Linux,
`%LOCALAPPDATA%\auriga-export` sous Windows, `~/Library/Application Support/...`
sous macOS), donc les fois suivantes la session est en général encore valide et
l'export démarre tout seul.

## Import dans Google Agenda

Paramètres → **Importer et exporter** → **Importer** → choisir `auriga.ics`.
Mets-le dans un agenda dédié (ex. « ISAE »).

Les UID sont stables : **réimporter un fichier mis à jour met à jour les événements
existants, sans doublon.** Relance l'export quand le planning se remplit.
Seule limite : un cours supprimé dans Auriga n'est pas retiré de Google, à faire à la main.

## Options

```bash
--list                  affiche le planning sans écrire de fichier
--start / --end         période (défaut : année universitaire en cours)
-o fichier.ics          fichier de sortie
--prefix-type           titres du type « [PC] 3A LV1-ANGLAIS »
--reminder 15           rappel 15 min avant chaque cours
--all-groups            liste toutes les filières au lieu des 3 premières
--no-allday-holidays    garde VACANCES/FÉRIÉ en horaires au lieu de journée entière
--json fichier.json     dump brut de l'API
--keep-browser          laisse le navigateur ouvert à la fin
--browser / --profile / --port   chemin du navigateur, profil, port de debug
```

## Contenu des événements

Titre (préfixe `EXAM - ` si examen), horaires, salles (`AMPHI 3`, `61.101 J2 SPA`),
intervenants, type d'activité en catégorie, filières/parcours, domaine, n° Auriga.
Les marqueurs `VACANCES` / `FÉRIÉ` deviennent des journées entières en temps libre.

## Fonctionnement

Le planning vient de `GET /api/plannings/me?days=1..7&startDate=…&endDate=…`, protégé
par un jeton Bearer OIDC que l'app Angular garde **en mémoire** (ni cookie, ni
localStorage) — il n'existe donc pas d'URL iCal à donner à Google.

Le script pilote le navigateur via le protocole DevTools, lit l'en-tête
`Authorization` sur une requête de l'app, puis interroge l'API directement depuis
Python. Il n'actualise jamais la page pendant que tu tapes tes identifiants ; une
fois l'app chargée il la relance seulement par changement d'ancre. Le jeton dure
~15 min et est renouvelé automatiquement si l'export est plus long.

Les données sont récupérées par fenêtres de 7 jours (comme l'interface web) et
dédoublonnées par identifiant.
