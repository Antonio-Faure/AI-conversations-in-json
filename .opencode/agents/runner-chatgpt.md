---
description: Pilote la conversation d'etalonnage ChatGPT (envoi resumable des messages/medias, reprise apres rate-limit).
mode: subagent
model: opencode-go/deepseek-v4.1-flash
steps: 200
permissions:
  - action: edit
    resource: "*"
    effect: allow
  - action: shell
    resource: "*"
    effect: allow
  - action: external_directory
    resource: "/home/*/Documents/code/aicv-wt/*"
    effect: allow
  - action: external_directory
    resource: "/home/*/Documents/code/aicv-run/*"
    effect: allow
---

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **ChatGPT**.

- Worktree : `${AICV_WT}/chatgpt`
- Runtime : `${AICV_RUN}/chatgpt`
- Driver : `src/drivers/chatgpt.py` ; parser : `src/parsers/chatgpt.py`
- Envoi en Playwright (moteur par defaut).
- Commande : `scripts/etalon_run.py --bot chatgpt [--url <url>]`.

Peux modifier `src/drivers/chatgpt.py` et `src/parsers/chatgpt.py` (+ tests).
Fichiers partages interdits (`src/drivers/base.py`, `runner.py`,
`src/parsers/base.py`, `src/schema.py`). Au rate-limit : s'arreter, laisser
`BILAN.md`/`state.json` a jour.
