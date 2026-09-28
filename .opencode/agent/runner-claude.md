---
description: Pilote la conversation d'etalonnage Claude (envoi resumable, reprise apres rate-limit).
mode: subagent
model: opencode-go/deepseek-v4.1-flash
temperature: 0.1
steps: 200
permission:
  edit: allow
  bash:
    "*": allow
  external_directory:
    "/home/*/Documents/code/aicv-wt/**": allow
    "/home/*/Documents/code/aicv-run/**": allow
---

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Claude**.

- Worktree : `${AICV_WT}/claude`
- Runtime : `${AICV_RUN}/claude`
- Driver : `src/drivers/claude.py` ; parser : `src/parsers/claude.py`
- Envoi en **botasaurus** (Cloudflare).
- Commande : `scripts/etalon_run.py --bot claude [--url <url>]`.

Peux modifier `src/drivers/claude.py` et `src/parsers/claude.py` (+ tests).
Fichiers partages interdits. Au rate-limit : s'arreter et laisser
`BILAN.md`/`state.json` a jour.
