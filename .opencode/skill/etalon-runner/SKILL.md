---
name: etalon-runner
description: Piloter la conversation d'etalonnage d'un chatbot (extraire la file de messages, generer les medias, envoyer via le driver, reprendre apres rate-limit avec BILAN). Use when running/driving the calibration conversation for a chatbot, sending test messages with attachments, or resuming after rate limits.
---

# Runner de conversation d'etalonnage

Objectif : **envoyer** dans un chatbot la liste de messages de test (avec
pieces jointes), de facon resumable, jusqu'a epuisement du rate-limit, puis
laisser un `BILAN.md` pour la reprise.

## Dossiers

- **Code (worktree)** : `/home/odin/Documents/code/aicv-wt/<bot>` (branche
  `parser/<bot>`) — chemins absolus, `bash` avec `workdir`.
- **Runtime** : `/home/odin/Documents/code/aicv-run/<bot>/`
  - `queue.json` : file des messages a envoyer
  - `state.json` : etat machine (index suivant, url cible, statut)
  - `BILAN.md` : bilan humain (progression, restants, reprise)
  - `attachments/` : medias a joindre
  - `exports/` (optionnel) : JSON/HTML/screenshots de la conversation
- Python : `/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python`
- Config partagee : `-c /home/odin/Documents/code/AI-conversations-in-json/config.yaml`

## Etape 1 — scraper les 5 dernieres conversations

```bash
/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python run.py \
  -c /home/odin/Documents/code/AI-conversations-in-json/config.yaml \
  --output /home/odin/Documents/code/aicv-wt/<bot>/exports \
  --monthly --service <bot> --limit 5 --screenshots
```

Identifier dans ces conversations :
- la conversation de **cadrage** : 2 messages, dont le **second** (assistant)
  contient la **liste des messages a envoyer** au chatbot ;
- pour **gemini et grok** : la conversation d'**etalon deja entamee** par
  l'utilisateur (a reprendre apres le dernier message deja envoye).

Si la conversation de cadrage est absente : **STOP**, ne rien envoyer, le dire.

## Etape 2 — construire `queue.json`

Extraire la liste du dernier message assistant et ecrire, dans le dossier
runtime :

```json
{
  "source_conversation": "https://...",
  "messages": [
    {"text": "premier message", "attachments": []},
    {"text": "regarde cette image", "attachments": ["image.png"]},
    {"text": "analyse cet audio", "attachments": ["audio.mp3"]}
  ]
}
```

Une entree = un envoi. `attachments` liste des noms de fichiers presents dans
`<run_dir>/attachments/`. Ne pas inventer de messages : suivre la liste du
chatbot.

## Etape 3 — medias

```bash
/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python \
  scripts/make_media.py --bot <bot>
```

Genere `image.png`, `audio.mp3`, `video.mp4`, `document.pdf`, `donnees.csv`,
`notes.txt`, `notes.md`, `donnees.json` dans `<run_dir>/attachments/`.

## Etape 4 — envoyer (resumable)

Valider d'abord la mecanique sans rien envoyer :

```bash
/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python \
  scripts/etalon_run.py --bot <bot> --dry-run
```

Puis envoyer (gemini/grok : reprendre l'etalon existant avec `--url`) :

```bash
/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python \
  scripts/etalon_run.py --bot <bot> [--url <url_etalon>]
```

- Le runner envoie **un message a la fois**, attend la reponse, met a jour
  `state.json`, et **s'arrete** des qu'un rate-limit est detecte.
- Reprise = relancer la meme commande : il repart a `next_index` (jamais de
  doublon).

## Etape 5 — bilan / reprise

- Statut `done` : tous les messages sont partis. Scraper l'etalon (JSON/HTML +
  screenshots) et le rapporter.
- Statut `rate_limited` : `BILAN.md` + `state.json` sont a jour. **S'arreter la,
  c'est normal.** Ne pas forcer. La reprise se fera au prochain lancement
  (toutes les ~24 h).
- Statut `error` : lire `last_error`, corriger (selecteurs du driver
  `src/drivers/<bot>.py`, upload, etc.), puis relancer.

## Si un selecteur ne marche plus

Corriger `src/drivers/<bot>.py` (selecteurs de champ/envoi/fichier/stop) en
s'aidant des screenshots et du DOM. Les fichiers `src/drivers/base.py` et
`src/drivers/runner.py` sont **partages** : ne pas les modifier sans le
signaler.

## Rapport final (obligatoire)

Retourner : bot, statut final, progression x/y, URL de l'etalon, ce qui a ete
envoye, erreurs/rate-limits rencontres, correctifs de code eventuels, et ce
qu'il reste a faire.
