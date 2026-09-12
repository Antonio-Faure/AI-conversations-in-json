---
description: Pilote la conversation d'etalonnage Grok (envoi resumable des messages/medias, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Grok**.

- Worktree : `/home/odin/Documents/code/aicv-wt/grok`
- Runtime : `/home/odin/Documents/code/aicv-run/grok`
- Driver : `src/drivers/grok.py` ; parser : `src/parsers/grok.py`
- Grok passe par le navigateur **botasaurus** pour l'envoi (Cloudflare) ; la
  lecture reste l'API. Pas de screenshots cote lecture.
- Grok est **deja entame** : reprendre la conversation d'etalon existante
  (`--url`) plutot que d'en creer une nouvelle.
- Commande : `scripts/etalon_run.py --bot grok [--url <url>]`.

Tu peux modifier `src/drivers/grok.py` et `src/parsers/grok.py` (+ tests).
Ne touche pas aux fichiers partages (`src/drivers/base.py`, `runner.py`,
`src/parsers/base.py`, `src/schema.py`). Au rate-limit : t'arreter et laisser
`BILAN.md`/`state.json` a jour.
