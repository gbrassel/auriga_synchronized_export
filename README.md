# 📅 Auriga ➡️ Calendrier (Synchronisation Automatisée)

Ce projet automatise l'exportation de votre emploi du temps Auriga (ISAE-SUPAERO) vers un calendrier tiers (Outlook, google calendar ou tout autre webmail, en maintenant une synchronisation régulière et invisible en arrière-plan.

L'architecture repose sur **GitHub Actions** pour exécuter le script, **Playwright** pour passer le SSO de l'école, et un **GitHub Gist secret** pour héberger dynamiquement le fichier `.ics`.

---

## 🏗️ Architecture du système

1. **Extraction (Playwright) :** Un navigateur Chromium "headless" (invisible) est lancé par GitHub Actions. Il navigue sur Auriga, remplit vos identifiants SSO, et intercepte le jeton de sécurité (`OIDC token`) échangé par l'application Angular.
2. **Génération (Python) :** Le script interroge l'API Auriga avec ce jeton, nettoie les données, extrait les informations pertinentes (Salles, Intervenants, Modules pédagogiques) et génère un fichier `auriga.ics` propre et normé.
3. **Hébergement (GitHub Gist) :** Le script met à jour un "Gist" secret sur votre compte GitHub via l'API GitHub.
4. **Synchronisation (Outlook) :** Les serveurs de Microsoft lisent régulièrement ce lien Gist pour mettre à jour votre emploi du temps Outlook (bureau, web et mobile).

---

## 📦 Dépendances et Prérequis

Le script Python utilise uniquement la bibliothèque standard, à l'exception du module d'automatisation web.

*   **Python 3.8+**
*   **Playwright** (pour l'authentification SSO via navigateur headless)

**Pour le déploiement sur GitHub Actions :**
Votre dépôt doit simplement contenir un fichier `requirements.txt` avec la ligne suivante :
````text
playwright
````
Le workflow GitHub Actions se chargera d'installer Python, Playwright, et les navigateurs nécessaires automatiquement à chaque exécution.

**Pour tester localement sur votre machine :**
````bash
pip install playwright
playwright install chromium
python auriga_export.py
````

---

## 🚀 Guide d'installation

Pour déployer votre propre instance de ce synchroniseur, suivez ces étapes :

### 1. Préparer l'hébergement (GitHub Gist)
1. Allez sur [gist.github.com](https://gist.github.com/) (connecté à votre compte GitHub).
2. Créez un nouveau Gist :
   - Description : *Planning ISAE*
   - Nom du fichier : `auriga.ics`
   - Contenu : Mettez un texte temporaire (ex: `En attente de synchronisation...`).
3. Cliquez impérativement sur **Create secret gist**.
4. Dans la barre d'adresse de votre navigateur, copiez l'ID du Gist (la longue chaîne alphanumérique après votre nom d'utilisateur, ex: `github.com/votre-nom/`**`ceci-est-lid-du-gist`**). Gardez-le de côté.

### 2. Créer un jeton d'accès GitHub (PAT)
1. Allez dans vos paramètres GitHub : **Settings > Developer settings > Personal access tokens > Tokens (classic)**.
2. Cliquez sur **Generate new token (classic)**.
3. Nommez-le (ex: *Auriga Sync*), mettez une expiration (ex: *No expiration*).
4. Cochez **uniquement la case `gist`** dans la liste des permissions.
5. Générez le token et **copiez-le immédiatement** (il ne sera plus jamais affiché).

### 3. Configurer le dépôt GitHub Actions
1. Forkez ou clonez ce dépôt sur votre propre compte GitHub. Assurez-vous que les fichiers `auriga_export.py`, `.github/workflows/sync.yml` et `requirements.txt` sont présents.
2. Allez dans l'onglet **Settings** de votre dépôt > **Secrets and variables** > **Actions**.
3. Ajoutez les 4 secrets suivants (`New repository secret`) :
   - `ISAE_USER` : Votre identifiant ISAE (ex: `prenom.nom`).
   - `ISAE_PASS` : Votre mot de passe ISAE.
   - `GIST_ID` : L'ID du Gist copié à l'étape 1.
   - `GIST_TOKEN` : Le jeton d'accès GitHub copié à l'étape 2.

### 4. Lancer la première synchronisation
1. Allez dans l'onglet **Actions** de votre dépôt.
2. Sélectionnez le workflow (ex: *Auriga Outlook Sync*).
3. Cliquez sur **Run workflow** (assurez-vous d'être sur la branche principale, ex: `main`).
4. Attendez que le voyant passe au vert (environ 1 minute). Votre Gist est maintenant rempli avec votre emploi du temps !

---

## 📆 Configuration dans votre application de calendrier

**⚠️ ATTENTION : Ne téléchargez PAS le fichier `.ics` pour l'importer manuellement.** Une importation statique ne se met pas à jour automatiquement et génère des doublons lors des réimports.

Vous devez impérativement vous **abonner au flux dynamique** via son URL :

### 1. Récupérer l'URL du flux
1. Rendez-vous sur votre Gist secret.
2. Cliquez sur le bouton **Raw** en haut à droite de l'encart `auriga.ics`.
3. Copiez l'URL dans la barre d'adresse.
   *💡 Important : supprimez le hash de révision dans l'URL afin qu'elle pointe toujours vers la dernière version générée : `https://gist.githubusercontent.com/<utilisateur>/<gist_id>/raw/auriga.ics`.*

### 2. S'abonner dans votre client de calendrier

* **Outlook (Web / Nouveau Outlook) :** Dans [Outlook Calendrier](https://outlook.office.com/calendar), cliquez sur **Ajouter un calendrier** → **S'abonner à partir du web**, collez l'URL, choisissez un nom (ex. *Cours ISAE*), puis cliquez sur **Importer**.
* **Google Agenda :** Sur [calendar.google.com](https://calendar.google.com), cliquez sur le **`+`** à côté d'*Autres agendas* → **À partir de l'URL**, collez l'URL et cliquez sur **Ajouter un agenda**.
* **Apple Calendrier (macOS / iOS) :**
  * *macOS :* Menu **Fichier** → **Nouvel abonnement à un calendrier…**, collez l'URL et choisissez la fréquence d'actualisation.
  * *iOS :* **Réglages** → **Apps** → **Calendrier** → **Comptes** → **Ajouter un compte** → **Autre** → **Ajouter un calendrier avec abonnement**.
* **Thunderbird et autres clients (Nextcloud, Proton, etc.) :** Sélectionnez **Nouvel agenda** → **Sur le réseau** (ou *S'abonner via URL / iCal*) et collez l'URL du Gist.

### ⏱️ Délais de synchronisation
La fréquence de mise à jour des abonnements web est gérée par votre service de calendrier, indépendamment de GitHub Actions :
* **Outlook Web :** toutes les 3 à 12 heures (sur *Outlook Classic* Windows, la touche `F9` permet de forcer la synchronisation).
* **Google Agenda :** toutes les 12 à 24 heures (non configurable).
* **Apple Calendrier / Thunderbird :** intervalle configurable manuellement (ex. toutes les heures).

---

## 🛠️ Dépannage et Fonctionnement

### Fréquence d'exécution
Le script est configuré pour s'exécuter automatiquement via le planificateur (`cron`) dans le fichier `.github/workflows/sync.yml`. 
*Note : Sur les comptes GitHub gratuits, l'heure exacte d'exécution peut glisser de quelques minutes selon la charge des serveurs.*

### Débogage SSO (La capture d'écran)
Si l'authentification échoue (mot de passe expiré, page ISAE en maintenance, demande de double facteur), le script génère automatiquement une capture d'écran de ce que "voit" le robot.
Pour la consulter :
1. Allez dans l'onglet **Actions** et cliquez sur l'exécution ayant échoué.
2. Tout en bas de la page *Summary*, téléchargez l'artefact **`debug-screenshot`** (fichier `.zip`).
3. Ouvrez le fichier `error.png` à l'intérieur pour comprendre ce qui a bloqué la connexion.

### Titres et Formatage
Le script effectue un nettoyage intelligent des données brutes d'Auriga :
- Les salles multiples ou couplées sont correctement extraites.
- Les cours annulés sont préfixés par `[ANNULÉ]`.
- Les examens sont préfixés par `[EXAMEN]`.
- Le titre du cours privilégie l'Unité Pédagogique complète (ex: *Représentation et analyse des systèmes dynamiques (Bureau d'études)*) plutôt que les titres génériques.

---

## 🤝 Contributeurs & Contact

* **Gauthier Brassel** ([@gbrassel](https://github.com/gbrassel)) — Automatisation GitHub Actions / Playwright, hébergement dynamique via Gist, normalisation des titres et synchronisation multi-calendriers.

### Contribuer
L'interface d'Auriga ou le portail SSO de l'ISAE-SUPAERO peuvent évoluer. Si vous rencontrez un bug ou souhaitez proposer une amélioration (ex. formatage des salles, ajout de rappels `VALARM`, filtres par matière) :
1. Ouvrez une **Issue** pour signaler le problème.
2. Proposez une **Pull Request** directement sur ce dépôt modèle.
