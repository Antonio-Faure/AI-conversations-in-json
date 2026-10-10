---
description: Corrige le parseur Grok (conversation etalon, sans screenshots car API). A utiliser pour reparer le parsing de grok uniquement.
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

Charge la skill `parser-loop` (outil `skill`) puis applique-la a **Grok**.

- Parseur : `src/parsers/grok.py`
- Service : `src/services/grok.py`
- Worktree : `${AICV_WT}/grok` (branche `parser/grok`)
- Commande de scrape : `--service grok --match "conversation etalon" --screenshots`.

**Important** : Grok passe par l'API HTTP (pas de navigateur), donc **aucun
screenshot** n'est produit. Travaille a partir du JSON, du HTML (`html_render`)
et de l'etalon que l'utilisateur te fournit. Points specifiques : reponse
markdown, `clean_grok_text`, blocs de code, LaTeX, listes, images.
