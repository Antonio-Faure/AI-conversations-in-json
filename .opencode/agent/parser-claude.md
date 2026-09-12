---
description: Corrige le parseur Claude (boucle etalon + screenshots). A utiliser pour reparer le scraping/parsing de claude uniquement.
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

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Claude**.

- Parseur : `src/parsers/claude.py`
- Service : `src/services/claude.py`
- Worktree : `/home/odin/Documents/code/aicv-wt/claude` (branche `parser/claude`)
- Selecteurs de tours : `parser.TURN_SELECTOR` / `parser.message_selectors`
  (`data-testid='user-message'`, `font-claude-response`, `collapsible-text`...).
- Commande de scrape : `--service claude --match "conversation etalon" --screenshots`.

Points specifiques Claude : blocs de reflexion/thinking (`THINKING_SELECTORS`),
`font-claude-message` vs `font-claude-response`, tableaux/code, alternance des
roles, Cloudflare (le service tourne en botasaurus).
