---
description: Corrige le parseur Mistral (boucle etalon + screenshots, application /work unique). A utiliser pour reparer le scraping/parsing de mistral uniquement.
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
---

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Mistral**.

- Parseur : `src/parsers/mistral.py`
- Service : `src/services/mistral.py`
- Worktree : `${AICV_WT}/mistral` (branche `parser/mistral`)
- Selecteurs de tours : `parser.message_selectors` (`div[data-message-author-role]`).
- Commande de scrape : `--service mistral --match "conversation etalon" --screenshots`.

Specifique Mistral : application unique **`/work`** (l'ancien `/chat` a
fusionne dedans ; les URLs `/chat/<id>` sont reecrites en `/work/<id>`).
Plusieurs conversations etalons peuvent exister : traite et verifie **chacune**
avant de conclure. Attention au nettoyage `clean_mistral_text` (math/`pre`).
