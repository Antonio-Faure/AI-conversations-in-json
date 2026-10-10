---
description: Pilote la conversation d'etalonnage Perplexity (envoi resumable, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Perplexity**.

- Worktree : `${AICV_WT}/perplexity`
- Runtime : `${AICV_RUN}/perplexity`
- Driver : `src/drivers/perplexity.py` ; parser : `src/parsers/perplexity.py`
- Envoi en **botasaurus** (Cloudflare).
- Commande : `scripts/etalon_run.py --bot perplexity [--url <url>]`.

Peux modifier `src/drivers/perplexity.py` et `src/parsers/perplexity.py`
(+ tests). Fichiers partages interdits. Le forfait gratuit rate-limite vite :
s'arreter proprement au rate-limit et laisser `BILAN.md`/`state.json` a jour.
