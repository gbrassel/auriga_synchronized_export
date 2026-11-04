# 📅 Auriga ➡️ Outlook (Synchronisation Automatisée)

Ce projet automatise l'exportation de votre emploi du temps Auriga (ISAE-SUPAERO) vers un calendrier Outlook, en maintenant une synchronisation régulière et invisible en arrière-plan.

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

## 📆 Configuration dans Outlook

**⚠️ ATTENTION : Ne téléchargez PAS le fichier `.ics` pour l'importer manuellement. Cela créerait des événements statiques impossibles à mettre à jour et générerait des doublons.**

Vous devez vous **abonner** au flux dynamique :

1. Retournez sur votre Gist secret.
2. Cliquez sur le bouton **Raw** en haut à droite de l'encart de code.
3. Copiez l'URL complète dans la barre de votre navigateur (`https://gist.githubusercontent.com/...`).
4. Ouvrez [Outlook Web (Calendrier)](https://outlook.office.com/calendar).
5. Dans le menu de gauche, cliquez sur **Ajouter un calendrier**, puis sur **S'abonner à partir du web**.
6. Collez l'URL de votre Gist.
7. Donnez-lui un nom (ex: *Cours ISAE*), une couleur, et une icône.
8. Cliquez sur **Importer**.

*Note : Outlook web met à jour les abonnements web toutes les 3 à 12 heures. Si vous utilisez l'application de bureau Outlook Classic sur Windows, vous pouvez forcer la synchronisation immédiatement en appuyant sur la touche `F9` (Envoyer/Recevoir).*

** L'URL du gist fonctionne aussi sur les autres applications de messagerie (google calendar, etc...). Il suffit de sélectionner l'option "Abonnement à partir du web". **

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
