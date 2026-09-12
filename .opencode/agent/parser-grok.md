---
description: Corrige le parseur Grok (conversation etalon, sans screenshots car API). A utiliser pour reparer le parsing de grok uniquement.
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

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Grok**.

- Parseur : `src/parsers/grok.py`
- Service : `src/services/grok.py`
- Worktree : `/home/odin/Documents/code/aicv-wt/grok` (branche `parser/grok`)
- Commande de scrape : `--service grok --match "conversation etalon" --screenshots`.

**Important** : Grok passe par l'API HTTP (pas de navigateur), donc **aucun
screenshot** n'est produit. Travaille a partir du JSON, du HTML (`html_render`)
et de l'etalon que l'utilisateur te fournit. Points specifiques : reponse
markdown, `clean_grok_text`, blocs de code, LaTeX, listes, images.
