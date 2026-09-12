---
description: Pilote la conversation d'etalonnage Perplexity (envoi resumable, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Perplexity**.

- Worktree : `/home/odin/Documents/code/aicv-wt/perplexity`
- Runtime : `/home/odin/Documents/code/aicv-run/perplexity`
- Driver : `src/drivers/perplexity.py` ; parser : `src/parsers/perplexity.py`
- Envoi en **botasaurus** (Cloudflare).
- Commande : `scripts/etalon_run.py --bot perplexity [--url <url>]`.

Peux modifier `src/drivers/perplexity.py` et `src/parsers/perplexity.py`
(+ tests). Fichiers partages interdits. Le forfait gratuit rate-limite vite :
s'arreter proprement au rate-limit et laisser `BILAN.md`/`state.json` a jour.
