---
description: Corrige le parseur ChatGPT (boucle etalon + screenshots). A utiliser pour reparer le scraping/parsing de chatgpt uniquement.
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
---

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **ChatGPT**.

- Parseur : `src/parsers/chatgpt.py`
- Service : `src/services/chatgpt.py`
- Worktree : `${AICV_WT}/chatgpt` (branche `parser/chatgpt`)
- Selecteurs de tours : `parser.message_selectors` (contient `[data-testid^='conversation-turn']`
  et `[data-message-author-role]`).
- Commande de scrape : `--service chatgpt --match "conversation etalon" --screenshots`.

Points specifiques ChatGPT : tours image sans `data-message-author-role`,
pieces jointes dans un `<button>`, `images.openai.com` / `estuary`, modele
affiche dans l'en-tete, alternance des roles.
