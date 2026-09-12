---
description: Corrige le parseur Perplexity (boucle etalon + screenshots). A utiliser pour reparer le scraping/parsing de perplexity uniquement.
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

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Perplexity**.

- Parseur : `src/parsers/perplexity.py`
- Service : `src/services/perplexity.py`
- Worktree : `/home/odin/Documents/code/aicv-wt/perplexity` (branche `parser/perplexity`)
- Selecteurs de tours : `USER_SELECTORS`, `ASSISTANT_SELECTORS`,
  `SOURCE_SELECTORS`, `parser.message_selectors`.
- Commande de scrape : `--service perplexity --match "conversation etalon" --screenshots`.

Points specifiques Perplexity : citations inline (`span.citation`) et sources
en bas, listes, tableaux, LaTeX, images S3 (`ppl-ai-file-upload`), rate-limit
du forfait gratuit (prevoir un nouveau essai).
