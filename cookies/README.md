# Cookies d'authentification

Ce dossier contient les cookies de session pour les scrapers (Grok, Mistral).
Les fichiers `*.json` ne sont **jamais** versionnés (`cookies/*.json` est
ignoré par git) — ne les commite pas et ne les partage pas.

## Mise en place

### Option A (automatique) — capture des cookies

Ouvre un navigateur visible, connecte-toi, appuie sur Entrée : les cookies du
domaine sont écrits automatiquement dans le fichier.

```bash
/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python \
  /home/odin/Documents/code/AI-conversations-in-json/run.py --login grok
/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python \
  /home/odin/Documents/code/AI-conversations-in-json/run.py --login mistral
```

(ou `scripts/capture_cookies.py grok mistral`)

### Option B (manuelle) — copier/coller

1. Ouvre le site dans ton navigateur, connecte-toi.
2. DevTools > Application (ou Stockage) > Cookies > sélectionne le domaine.
3. Copie les valeurs des cookies d'authentification dans le fichier
   correspondant, par exemple `cookies/mistral.json` :

   ```json
   "cookies": {
     "next-auth.session-token": "COLLE_TA_VALEUR_ICI"
   }
   ```

### Vérification

Teste la session :

   ```bash
   /home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python \
     /home/odin/Documents/code/AI-conversations-in-json/scripts/check_cookies.py
   ```

Le script envoie les cookies via un fichier temporaire (permissions `600`) et
n'affiche jamais leur valeur.

## Champs du fichier

| champ | role |
|---|---|
| `app_url` | URL de l'application |
| `domain` | domaine des cookies (ex: `.mistral.ai`) |
| `check_url` | endpoint teste pour verifier l'authentification |
| `authenticated_markers` | sous-chaines presentes dans la reponse si connecte |
| `success_status` | codes HTTP consideres comme valides |
| `headers` | en-tetes additionnels (ex: `Authorization`) |
| `cookies` | dictionnaire nom -> valeur des cookies |
