---
description: Corrige le parseur ChatGPT (boucle etalon + screenshots). A utiliser pour reparer le scraping/parsing de chatgpt uniquement.
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
---

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **ChatGPT**.

- Parseur : `src/parsers/chatgpt.py`
- Service : `src/services/chatgpt.py`
- Worktree : `/home/odin/Documents/code/aicv-wt/chatgpt` (branche `parser/chatgpt`)
- Selecteurs de tours : `parser.message_selectors` (contient `[data-testid^='conversation-turn']`
  et `[data-message-author-role]`).
- Commande de scrape : `--service chatgpt --match "conversation etalon" --screenshots`.

Points specifiques ChatGPT : tours image sans `data-message-author-role`,
pieces jointes dans un `<button>`, `images.openai.com` / `estuary`, modele
affiche dans l'en-tete, alternance des roles.
