---
description: Construit le scrapeur API ChatGPT (backend-api) : client HTTP + mapper JSON, valide contre l'export DOM. A utiliser pour la migration API de chatgpt uniquement.
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

Charge la skill `api-loop` (outil `skill`) puis applique-la a **ChatGPT**.

- Service : `src/services/chatgpt.py` — ajouter le chemin API (opt-in config
  `services.chatgpt.api: true`, DOM par defaut). Parseur : `src/parsers/chatgpt.py`
  — mapper JSON -> `Conversation`.
- Worktree : `${AICV_WT}/chatgpt` (branche `api/chatgpt`).
- Les variables `AICV_ROOT`/`AICV_WT` ne sont pas definies : resoudre les
  chemins (racine du depot et dossier des worktrees) avant usage.
- REA : MCP pas encore charge dans la session -> utiliser le CLI
  (`PATH="$HOME/.local/share/node24/bin:$PATH" npx -y rea-agents@6.4.0 inspect-web-network-capture <har> har --json`).

HTML genere (`html_render.py`, comme grok), pas de screenshots. Le DOM reste en
repli. Ne toucher que le service, le parseur et les tests de la tete.
