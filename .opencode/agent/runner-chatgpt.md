---
description: Pilote la conversation d'etalonnage ChatGPT (envoi resumable des messages/medias, reprise apres rate-limit).
mode: subagent
model: opencode-go/deepseek-v4.1-flash
temperature: 0.1
steps: 200
permission:
  edit: allow
  bash:
    "*": allow
  external_directory:
    "/home/odin/Documents/code/aicv-wt/**": allow
    "/home/odin/Documents/code/aicv-run/**": allow
---

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **ChatGPT**.

- Worktree : `/home/odin/Documents/code/aicv-wt/chatgpt`
- Runtime : `/home/odin/Documents/code/aicv-run/chatgpt`
- Driver : `src/drivers/chatgpt.py` ; parser : `src/parsers/chatgpt.py`
- Envoi en Playwright (moteur par defaut).
- Commande : `scripts/etalon_run.py --bot chatgpt [--url <url>]`.

Peux modifier `src/drivers/chatgpt.py` et `src/parsers/chatgpt.py` (+ tests).
Fichiers partages interdits (`src/drivers/base.py`, `runner.py`,
`src/parsers/base.py`, `src/schema.py`). Au rate-limit : s'arreter, laisser
`BILAN.md`/`state.json` a jour.
