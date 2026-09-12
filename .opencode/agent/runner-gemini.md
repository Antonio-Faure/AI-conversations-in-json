---
description: Pilote la conversation d'etalonnage Gemini (envoi resumable des messages/medias, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Gemini**.

- Worktree : `/home/odin/Documents/code/aicv-wt/gemini`
- Runtime : `/home/odin/Documents/code/aicv-run/gemini`
- Driver : `src/drivers/gemini.py` ; parser : `src/parsers/gemini.py`
- Gemini est **deja entame** : reprendre la conversation d'etalon existante
  (`--url`) plutot que d'en creer une nouvelle.
- Commande : `scripts/etalon_run.py --bot gemini [--url <url>]`.

Tu peux modifier `src/drivers/gemini.py` et `src/parsers/gemini.py` (+ tests).
Ne touche pas aux fichiers partages (`src/drivers/base.py`, `runner.py`,
`src/parsers/base.py`, `src/schema.py`). Au rate-limit : t'arreter et laisser
`BILAN.md`/`state.json` a jour.
