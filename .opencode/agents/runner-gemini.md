---
description: Pilote la conversation d'etalonnage Gemini (envoi resumable des messages/medias, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Gemini**.

- Worktree : `${AICV_WT}/gemini`
- Runtime : `${AICV_RUN}/gemini`
- Driver : `src/drivers/gemini.py` ; parser : `src/parsers/gemini.py`
- Gemini est **deja entame** : reprendre la conversation d'etalon existante
  (`--url`) plutot que d'en creer une nouvelle.
- Commande : `scripts/etalon_run.py --bot gemini [--url <url>]`.

Tu peux modifier `src/drivers/gemini.py` et `src/parsers/gemini.py` (+ tests).
Ne touche pas aux fichiers partages (`src/drivers/base.py`, `runner.py`,
`src/parsers/base.py`, `src/schema.py`). Au rate-limit : t'arreter et laisser
`BILAN.md`/`state.json` a jour.
