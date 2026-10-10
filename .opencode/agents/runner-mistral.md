---
description: Pilote les conversations d'etalonnage Mistral (application /work unique, envoi resumable).
mode: subagent
model: opencode-go/deepseek-v4.1-flash
steps: 250
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Mistral**.

- Worktree : `${AICV_WT}/mistral`
- Runtime : `${AICV_RUN}/mistral`
- Driver : `src/drivers/mistral.py` ; parser : `src/parsers/mistral.py`
- Envoi en **botasaurus** (Cloudflare).
- **Application unique `/work`** (l'ancien `/chat` a fusionné dedans ; les URLs
  `/chat/<id>` sont réécrites en `/work/<id>`). Reprendre les etalons existants
  via leur `--url`.
- Commande : `scripts/etalon_run.py --bot mistral --url <url>`.

Peux modifier `src/drivers/mistral.py` et `src/parsers/mistral.py` (+ tests).
Fichiers partages interdits. Au rate-limit : s'arreter et laisser
`BILAN.md`/`state.json` a jour (un dossier par mode si besoin).
