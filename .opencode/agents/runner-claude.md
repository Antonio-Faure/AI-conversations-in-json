---
description: Pilote la conversation d'etalonnage Claude (envoi resumable, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Claude**.

- Worktree : `${AICV_WT}/claude`
- Runtime : `${AICV_RUN}/claude`
- Driver : `src/drivers/claude.py` ; parser : `src/parsers/claude.py`
- Envoi en **botasaurus** (Cloudflare).
- Commande : `scripts/etalon_run.py --bot claude [--url <url>]`.

Peux modifier `src/drivers/claude.py` et `src/parsers/claude.py` (+ tests).
Fichiers partages interdits. Au rate-limit : s'arreter et laisser
`BILAN.md`/`state.json` a jour.
