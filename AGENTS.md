# AGENTS.md

Guide pour les agents IA qui travaillent sur ce dépôt.

## En bref

Scraping de 6 chatbots (**chatgpt, claude, gemini, perplexity, grok, mistral**)
vers un **JSON standardisé + le HTML brut** de chaque conversation, plus un RAG
et une page web de recherche. Le détail complet est dans [`README.md`](README.md) ;
ce fichier donne les conventions et le mode opératoire.

## Environnement

- Python du venv **toujours** : `.venv/bin/python` (chemins **absolus**).
- Aucun secret commité : `cookies/*.json`, `profiles/`, `exports/`, `rag/`,
  `logs/` sont gitignorés. Ne jamais afficher/copier un cookie ou un token.
- Config : [`config.yaml`](config.yaml) (chemins absolus) ; les valeurs par
  défaut sont dans `DEFAULT_CONFIG` (`src/orchestrator.py`).

## Commandes

```bash
# Tests + lint (obligatoire avant tout commit)
.venv/bin/python -m pytest -q
.venv/bin/python -m pyflakes src run.py scripts tests

# Scraping
.venv/bin/python run.py --daily
.venv/bin/python run.py --monthly --service gemini
.venv/bin/python run.py --monthly --match "conversation etalon"   # filtre titre (sans accents)
.venv/bin/python run.py --daily --headful --limit 5 --verbose     # debug
.venv/bin/python run.py --login claude                            # 1re connexion (profil)

# Régénérer les JSON depuis les HTML (sans re-crawler), puis réindexer
.venv/bin/python scripts/reparse_exports.py
.venv/bin/python scripts/rag_index.py --force

# RAG + web
.venv/bin/python scripts/rag_index.py [--platform grok] [--force]
.venv/bin/python scripts/rag_search.py "requête" -k 10
.venv/bin/python scripts/serve_web.py            # http://127.0.0.1:8765

# Utilitaires
.venv/bin/python scripts/backfill_urls.py        # champ url manquant
.venv/bin/python scripts/download_images.py      # images locales (best effort)
.venv/bin/python scripts/random_conversation.py
.venv/bin/python scripts/audit_exports.py exports
```

Options récentes utiles : `--match TEXTE` (ne scraper que les titres/id
correspondants), `--screenshots` (un PNG par tour, centré, dans
`exports/<platform>/screenshots/<conv>/message-NN.png`), `--output DIR`
(surcharge `output_dir`).

## Architecture (points d'entrée)

- **CLI** : `run.py` → `Orchestrator.run/run_service/_scrape_one`
  (`src/orchestrator.py`). `_scrape_one` gère l'écriture JSON/HTML, la mise à
  jour de `conversation_list.json`, le téléchargement d'images et les screenshots.
- **Services** (navigation + découverte) : `src/services/<bot>.py`, façades
  communes `scrape_conversation` / `parse_page` / `capture_message_screenshots`
  dans `src/services/base.py`. Grok est **API + cookies** (`uses_browser=False`).
- **Parseurs** (DOM → `Conversation`, testables sans navigateur) :
  `src/parsers/<bot>.py`, helpers dans `src/parsers/base.py`.
- **Schéma** : `src/schema.py` (`Conversation`, `Message`, `normalize_messages`).
- **Navigateurs** : `src/browser.py` (Playwright) et
  `src/browser_botasaurus.py` (anti-Cloudflare) — même façade ; les services ne
  doivent pas accéder à `session.page`.
- **RAG** : `src/rag/` (BGE-M3 + SQLite/sqlite-vec), page web `web/`, serveur
  `scripts/serve_web.py`.

## Invariants à ne pas casser

- **Schéma JSON figé** : ordre des clés `conversation_id, platform, title, url,
  model, started_at, last_message_at, exported_at, messages`. Ne jamais renommer
  un champ.
- **Alternance des rôles** : jamais deux `user` ni deux `assistant` consécutifs
  (`normalize_messages`). Les tours éclatés sont fusionnés, les doublons retirés.
- `texte` = markdown complet (fences incluses) ; `code_blocks` = vue structurée
  `{language, code}` (le code reste aussi dans `texte`).
- **Images** : markdown relatif `![alt](images/<hash>.<ext>)`, fichier téléchargé
  dans `exports/<platform>/images/`, servi par `/media/<platform>/<fichier>`.
- **URL** de conversation toujours renseignée (`Conversation.url`).
- Ajouter une capacité de parseur = fichier `<bot>.py` + tests, **sans** toucher
  `base.py`/`schema.py`/serveur sans raison.

## Tests

- `pytest` (config `pytest.ini`, `pythonpath = .`). Fixtures HTML dans
  `tests/fixtures/` (fixture `fixture_html` dans `tests/conftest.py`).
- Tester les parseurs **sans navigateur** (HTML en dur) ; l'orchestrateur avec
  des services/sessions factices (`tests/test_orchestrator.py`) ; le CLI sans
  Chromium (`tests/test_cli.py`).
- Toute correction de parseur doit venir avec un test qui reproduit le cas.

## Sous-agents de correction de parseur

- Agents : `.opencode/agent/parser-<bot>.md` (modèle vision
  `opencode-go/deepseek-v4.1-flash`, outils complet).
- Skill commune : `.opencode/skill/parser-loop/SKILL.md` — boucle
  `scrape → screenshots → patch parseur → re-scrape`.
- Chaque agent travaille dans son **worktree** `/home/odin/Documents/code/aicv-wt/<bot>`
  et ne modifie que `src/parsers/<bot>.py` (+ service + tests). Les fichiers
  partagés (`base.py`, `schema.py`, `orchestrator.py`, `scripts/`, `web/`) sont
  réservés à l'agent principal.
- Les agents/skill ne sont chargés qu'au **démarrage** d'opencode : redémarrer
  après modification de `.opencode/`.

## Envoi de messages (conversation d'étalonnage)

- Drivers d'envoi : `src/drivers/<bot>.py` (saisie, upload, attente, rate-limit)
  au-dessus de la façade `session` (`click_any`/`type_into`/`upload_any`/`press`).
- Runner resumable : `src/drivers/runner.py` + `scripts/etalon_run.py`
  (`queue.json` + `state.json` + `BILAN.md`, arrêt sur rate-limit).
- Médias de test : `scripts/make_media.py` (`src/utils/media.py`, ffmpeg).
- Skill `etalon-runner` et agents `.opencode/agent/runner-<bot>.md` (gemini, grok).
- Dossiers runtime hors dépôt : `/home/odin/Documents/code/aicv-run/<bot>/`.

## Pièges connus

- DOM des plateformes volatil → chaînes de sélecteurs de repli ; mettre à jour
  `message_selectors` + parser en cas de rupture.
- Cloudflare : Claude/Perplexity/Mistral forcent `engine: botasaurus` ; login et
  export doivent utiliser **le même moteur** (cookies liés à l'UA).
- Perplexity gratuit rate-limite → espacer (`--daily`), pas de scraping massif.
- Mistral : **deux modes** `/chat` et `/work` (même titre possible) ; le mode est
  activé par `_ensure_mode`. Timestamps non exposés (`null`).
- Grok : pas de navigateur → pas de screenshots, HTML généré (`html_render.py`).
- Après un `reparse_exports.py` ou un `download_images.py`, relancer
  `rag_index.py --force` (cache par empreinte de message).

## Conventions

- Commentaires/docstrings en **français**, code et noms en anglais ; type hints.
- Pas de dépendance nouvelle sans la déclarer dans `requirements.txt`.
- Commits : `type(scope): résumé` en français, minuscule (ex.
  `fix(gemini): listes imbriquées`, `feat(web): ...`). Ne pas commit sans demande.
