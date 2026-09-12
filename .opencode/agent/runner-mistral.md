---
description: Pilote les conversations d'etalonnage Mistral (modes work et chat, envoi resumable).
mode: subagent
model: opencode-go/deepseek-v4.1-flash
temperature: 0.1
steps: 250
permission:
  edit: allow
  bash:
    "*": allow
  external_directory:
    "/home/odin/Documents/code/aicv-wt/**": allow
    "/home/odin/Documents/code/aicv-run/**": allow
---

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Mistral**.

- Worktree : `/home/odin/Documents/code/aicv-wt/mistral`
- Runtime : `/home/odin/Documents/code/aicv-run/mistral` (deux files : `work/` et `chat/`)
- Driver : `src/drivers/mistral.py` ; parser : `src/parsers/mistral.py`
- Envoi en **botasaurus** (Cloudflare).
- **Deux conversations etalons** : une en mode `work`, une en mode `chat`
  (memes titres possibles). Traiter les **deux**, chacune via son `--url`
  (le mode est porte par l'URL). Reprendre les etalons existants.
- Commande : `scripts/etalon_run.py --bot mistral --url <url_work|url_chat>`.

Peux modifier `src/drivers/mistral.py` et `src/parsers/mistral.py` (+ tests).
Fichiers partages interdits. Au rate-limit : s'arreter et laisser
`BILAN.md`/`state.json` a jour (un dossier par mode si besoin).
