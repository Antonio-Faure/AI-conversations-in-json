---
description: Pilote la conversation d'etalonnage Grok (envoi resumable des messages/medias, reprise apres rate-limit).
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

Charge la skill `etalon-runner` (outil `skill`) puis applique-la a **Grok**.

- Worktree : `${AICV_WT}/grok`
- Runtime : `${AICV_RUN}/grok`
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
