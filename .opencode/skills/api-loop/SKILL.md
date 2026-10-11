---
name: api-loop
description: Boucle de construction/correction d'un scrapeur API (client HTTP + mapper JSON) a partir d'un HAR capture (REA) et de l'export DOM comme verite terrain. Use when building or fixing an API-based scraper (src/services/<bot>.py + JSON mapper) for ChatGPT, Claude, Gemini, Perplexity, Mistral or Grok.
---

# Boucle scrapeur API (HAR -> client HTTP -> mapper JSON)

Modele de reference : Grok (`src/services/grok.py` + `src/parsers/grok.py`) —
API + cookies, aucun navigateur en lecture, HTML genere.

## Principe

L'API de la plateforme est la source ; le HAR capture (session connectee) est la
carte ; l'export DOM de l'etalon est la **verite terrain** pour valider le
mapping. On itere jusqu'a ce que le JSON API mappe concorde avec l'export DOM
(roles, texte markdown, listes, tableaux, code, LaTeX, images), puis on bascule
en repli DOM.

## Repertoire isole

Comme `parser-loop` : worktree git `${AICV_WT}/<bot>` (branche `api/<bot>`),
chemins absolus, `${AICV_ROOT}/.venv/bin/python`, `-c ${AICV_ROOT}/config.yaml`
(profils/cookies partages). Sortie : `--output ${AICV_WT}/<bot>/exports`.

## Recette

1. **Recon HAR** : capturer un HAR de la session connectee (Playwright
   `record_har_path` + `record_har_url_filter` sur les endpoints API, contenu
   `embed`) : liste + ouverture d'une conversation.
2. **Analyse REA** :
   `PATH="$HOME/.local/share/node24/bin:$PATH" npx -y rea-agents@6.4.0 inspect-web-network-capture <har> har --json`
   (corps conserves, cookies/auth masques). Cartographier endpoints, params,
   headers, payloads.
3. **Client HTTP** : cookies de la session (+ Bearer/account si requis), appels
   directs ; decouverte paginee par API ; pas de navigateur pour les lectures
   (une visite navigateur peut servir a frapper les cookies/token si besoin).
4. **Mapper JSON -> `Conversation`** (schema fige) : filtrer les messages non
   visibles (flags `hidden`, contextes injectes), regrouper tool/reasoning dans
   les extras du tour, markdown depuis les `parts`, `code_blocks`, images en
   `images/<hash>.<ext>`.
5. **Tests** sur **fixture JSON capturee** (hors navigateur) + comparaison a
   l'export DOM de l'etalon (compteurs, exemples).
6. `check_etalons.py` : baseline inchangee (le meme contenu doit sortir).
7. pyflakes + pytest complets ; commit sur la branche (ne pas push).

## Regles de donnees

- Schema JSON fige ; `texte` = markdown complet ; jamais deux roles consecutifs.
- HTML **genere** (`html_render.py`) — plus de HTML brut (decision actee) ; pas
  de screenshots.
- Le DOM reste en **repli** tant que l'API n'est pas validee (bascule opt-in).

## Perimetre STRICT

Modifiable : `src/services/<bot>.py`, `src/parsers/<bot>.py`, tests de la tete.
Interdits (agent principal) : `src/parsers/base.py`, `src/schema.py`,
`src/orchestrator.py`, `scripts/`, `web/`, `run.py` — decrire tout besoin.

## Rapport final (obligatoire)

Resume des correctifs, fichiers modifies, hash du commit, avant/apres sur
l'etalon (compteurs + exemples), tests ajoutes, points non resolus et besoins
dans les fichiers partages.
