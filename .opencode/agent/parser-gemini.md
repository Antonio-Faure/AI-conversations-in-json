---
description: Corrige le parseur Gemini (boucle etalon + screenshots). A utiliser pour reparer le scraping/parsing de gemini uniquement.
mode: subagent
model: opencode-go/deepseek-v4-flash-vision-exp
temperature: 0.1
steps: 200
permission:
  edit: allow
  bash:
    "*": allow
  external_directory:
    "/home/odin/Documents/code/aicv-wt/**": allow
---

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Gemini**.

- Parseur : `src/parsers/gemini.py`
- Service : `src/services/gemini.py`
- Worktree : `/home/odin/Documents/code/aicv-wt/gemini` (branche `parser/gemini`)
- Selecteurs de tours : `parser.message_selectors` (`user-query`, `model-response`,
  `message-content`) ; `USER_SELECTORS` / `ASSISTANT_SELECTORS`.
- Commande de scrape : `--service gemini --match "conversation etalon" --screenshots`.

Points specifiques Gemini : listes (le probleme remonte : « manque de liste »),
tableaux, `message-content`, images generees/`googleusercontent`, LaTeX,
footers/actions a exclure.
