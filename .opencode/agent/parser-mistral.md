---
description: Corrige le parseur Mistral (boucle etalon + screenshots, modes work et chat). A utiliser pour reparer le scraping/parsing de mistral uniquement.
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
---

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Mistral**.

- Parseur : `src/parsers/mistral.py`
- Service : `src/services/mistral.py`
- Worktree : `/home/odin/Documents/code/aicv-wt/mistral` (branche `parser/mistral`)
- Selecteurs de tours : `parser.message_selectors` (`div[data-message-author-role]`).
- Commande de scrape : `--service mistral --match "conversation etalon" --screenshots`.

Specifique Mistral : **deux conversations etalons** (une sur le mode `work`,
une sur le mode `chat`), meme titre. Le pipeline les scrape toutes les deux et
le service gere le changement de mode (`_ensure_mode`). Tu dois traiter et
verifier **les deux** avant de conclure. Attention au nettoyage
`clean_mistral_text` (math/`pre`).
