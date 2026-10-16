# AGENTS.md

Guide pour les agents IA qui travaillent sur ce dépôt.

## En bref

Scraping de 6 chatbots (**chatgpt, claude, gemini, perplexity, grok, mistral**)
vers un **JSON standardisé + l'artefact brut** de chaque conversation (HTML de
la page pour les plateformes DOM, payload API brut pour les plateformes API),
plus un RAG et une page web de recherche. Le détail complet est dans
[`README.md`](README.md) ; ce fichier donne les conventions et le mode opératoire.

## Environnement

- Python du venv **toujours** : `$AICV_ROOT/.venv/bin/python`.
- Variables de chemins (a definir dans l'environnement) : `AICV_ROOT` = racine
  du depot, `AICV_WT` = dossier des worktrees des sous-agents, `AICV_RUN` =
  dossiers runtime hors depot.
- Aucun secret commité : `cookies/*.json`, `profiles/`, `exports/`, `rag/`,
  `logs/` sont gitignorés. Ne jamais afficher/copier un cookie ou un token.
- Config : [`config.yaml`](config.yaml) (chemins relatifs au fichier) ; les
  valeurs par défaut sont dans `DEFAULT_CONFIG` (`src/orchestrator.py`).

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

# Régénérer les JSON depuis les artefacts bruts (HTML / payload API), puis réindexer
.venv/bin/python scripts/reparse_exports.py
.venv/bin/python scripts/rag_index.py --force

# Moniteur de similarité des API internes (forme value-free des payloads)
.venv/bin/python scripts/api_bench.py shape --platform chatgpt        # diff vs référence
.venv/bin/python scripts/api_bench.py shape --platform grok --live    # fetch étalon live
.venv/bin/python scripts/api_bench.py update-ref --platform claude    # réf. = étalons
.venv/bin/python scripts/api_bench.py update-ref --platform claude --all  # réf. = corpus
.venv/bin/python scripts/api_bench.py bench --platform chatgpt --n 3  # bench live
.venv/bin/python scripts/api_bench.py report                          # api_health.json

# RAG + web
.venv/bin/python scripts/rag_index.py [--platform grok] [--force]
.venv/bin/python scripts/rag_search.py "requête" -k 10
.venv/bin/python scripts/serve_web.py            # http://127.0.0.1:8765

# Medias EmbeddingGemma (meme index) : texte puis medias
.venv/bin/python scripts/rag_index_media.py      # images/audio/video -> vec_media
.venv/bin/python scripts/rag_index_media.py --search "un paysage" --kind image
.venv/bin/python scripts/prune_media.py          # elague les medias orphelins (dry-run)
.venv/bin/python scripts/purge_conversations.py --from-file LIST.json   # purge locale (dry-run)

# Serveur MCP stdio (recherche depuis un agent ; stdout = JSON-RPC, logs stderr)
.venv/bin/python scripts/mcp_conversations.py [-c config.yaml]

# Utilitaires
.venv/bin/python scripts/backfill_urls.py        # champ url manquant
.venv/bin/python scripts/download_images.py      # images locales (best effort)
.venv/bin/python scripts/random_conversation.py
.venv/bin/python scripts/audit_exports.py exports

# Contrôle de régression des conversations d'étalonnage (canaris des parseurs)
.venv/bin/python scripts/check_etalons.py            # compare à la baseline
.venv/bin/python scripts/check_etalons.py --update   # mémorise la baseline
```

Options récentes utiles : `--match TEXTE` (ne scraper que les titres/id
correspondants), `--screenshots` (un PNG par tour, centré, dans
`exports/<platform>/screenshots/<conv>/message-NN.png`), `--variants` (capture
les versions alternatives des messages : éditions/régénérations, champ
`variants` du JSON ; implémenté pour mistral et chatgpt — ChatGPT ouvre un
dialogue « Versions » Radix qui exige un clic souris réel (`session.click_at`)
et des libellés localisés FR/EN ; opt-in car plus lent), `--output DIR` (surcharge
`output_dir`), `--retry-unavailable` (réessaie les conversations marquées
indisponibles).

## Architecture (points d'entrée)

- **CLI** : `run.py` → `Orchestrator.run/run_service/_scrape_one`
  (`src/orchestrator.py`). `_scrape_one` gère l'écriture JSON + artefact brut
  (HTML ou payload API via `src/utils/raw_payload.py`), la mise à jour de
  `conversation_list.json`, le téléchargement d'images et les screenshots.
- **Services** (navigation + découverte) : `src/services/<bot>.py`, façades
  communes `scrape_conversation` / `parse_page` / `capture_message_screenshots`
  dans `src/services/base.py`. Grok est **API + cookies** (`uses_browser=False`) ;
  ChatGPT, Claude, Mistral et Perplexity sont en **mode API par défaut**
  (désactivable par `services.<bot>.api: false` → repli DOM ; pour Perplexity,
  `services.perplexity.dom: true`) ; Gemini est en mode API **opt-in**
  (`services.gemini.api: true`, actif dans `config.yaml`). Les services API
  renvoient `(conv, None)` et exposent `take_raw_payload()`.
- **Parseurs** (DOM → `Conversation`, testables sans navigateur) :
  `src/parsers/<bot>.py`, helpers dans `src/parsers/base.py`.
- **Schéma** : `src/schema.py` (`Conversation`, `Message`, `normalize_messages`).
- **Moniteur de forme API** : `src/utils/api_shape.py` (arbre value-free +
  `compare_shapes`), `ShapeMonitor` dans `src/services/base.py` (hook
  `record_api_shape` après chaque fetch API des 6 services), références
  versionnées `tests/fixtures/api_shapes/<bot>.shape.json`, CLI
  `scripts/api_bench.py` (`shape`/`update-ref`/`bench`/`report`).
- **Navigateurs** : `src/browser.py` (Playwright) et
  `src/browser_botasaurus.py` (anti-Cloudflare) — même façade ; les services ne
  doivent pas accéder à `session.page`.
- **RAG** : `src/rag/` (EmbeddingGemma 2 768 dims — `rag_model`/`rag_dim`
  configurables — + SQLite/sqlite-vec, médias via `src/rag/media.py`), page web
  `web/`, serveur `scripts/serve_web.py`.
- **Serveur MCP** : `scripts/mcp_conversations.py` (SDK `mcp` v2, transport
  stdio ; outils `search`, `grep`, `get_conversation`, `get_message`,
  `list_conversations`, `stats`), enregistré sous `mcp.servers.aicv` dans la
  config globale opencode ; skill `.opencode/skills/conversations-search/`.

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
- **Pièces jointes** (PDF/CSV/JSON/TXT/audio/vidéo) : lien relatif
  `[nom](files/<hash>.<ext>)`, fichier téléchargé dans `exports/<platform>/files/`,
  servi par la même route `/media/<platform>/<fichier>`.
- **URL** de conversation toujours renseignée (`Conversation.url`).
- **Artefact brut** : HTML (`<stem>.html`) pour les plateformes DOM (repli
  `api: false`). Pour les plateformes API (chatgpt, claude, gemini, grok,
  mistral, perplexity), le payload brut
  `exports/<platform>/raw/<slug>.json` remplace le HTML (re-mapping sans
  re-téléchargement) ; un `.html` résiduel est supprimé au prochain scrape et
  `reparse_exports.py` donne la priorité au payload brut.
- Ajouter une capacité de parseur = fichier `<bot>.py` + tests, **sans** toucher
  `base.py`/`schema.py`/serveur sans raison.

## Tests

- `pytest` (config `pytest.ini`, `pythonpath = .`). Fixtures HTML dans
  `tests/fixtures/` (fixture `fixture_html` dans `tests/conftest.py`) ; payloads
  API (`*_api_*.json`) pour tester les mappers JSON hors navigateur.
- Tester les parseurs **sans navigateur** (HTML en dur) ; l'orchestrateur avec
  des services/sessions factices (`tests/test_orchestrator.py`) ; le CLI sans
  Chromium (`tests/test_cli.py`).
- Toute correction de parseur doit venir avec un test qui reproduit le cas.

## Sous-agents de correction de parseur

- Agents : `.opencode/agents/parser-<bot>.md` (modèle vision
  `opencode-go/deepseek-v4.1-flash`, outils complet).
- Skill commune : `.opencode/skills/parser-loop/SKILL.md` — boucle
  `scrape → screenshots → patch parseur → re-scrape`.
- Chaque agent travaille dans son **worktree** `${AICV_WT}/<bot>`
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
- Skill `etalon-runner` et agents `.opencode/agents/runner-<bot>.md` (gemini, grok).
- Définitions au format natif OpenCode V2 (`agents/`, `skills/`, `permissions:`).
- Dossiers runtime hors dépôt : `${AICV_RUN}/<bot>/`.

## Conversation d'étalonnage (canari des parseurs)

- Liste suivie dans `scripts/etalons.json` (plateforme → id/label + baseline),
  non versionne ; modèle : `scripts/etalons.example.json`.
- `scripts/check_etalons.py` compare les métriques (messages, alternance des
  rôles, images, tableaux, listes, langues de code, LaTeX) à la baseline et sort
  en erreur si une capacité chute → à lancer après chaque `--monthly`.
- Quand une conversation d'étalonnage change (nouvel étalonnage), mettre à jour
  les id dans `scripts/etalons.json` puis `check_etalons.py --update`.

## Calibration (suite de tests compactée par bot)

- `calibration/manifest.json` : taxonomie fermée des capacités à exposer
  (`keywords` = taggage auto, `parsed_today` = déjà parsé ou non).
- `calibration/<bot>.json` : la conversation d'étalonnage compactée — les
  messages **générés par le bot lui-même** (« crée une suite de messages
  utilisateur pour tester toutes ces capacités »), réduits au **minimum qui
  couvre tout** (set-cover) et 100 % propres à ce bot.
- `calibration/bootstraps.json` : les 2 amorces (documentation + génération de
  suite) à envoyer **une fois** aux bots sans suite.
- `calibration/sources/<bot>.md` : suites exportées depuis un canvas/document
  (ChatGPT, Claude) quand le bot n'expose pas la suite dans un export JSON.
- `calibration/supplements.json` : tests de complément pour les capacités que la
  suite du bot ne couvre pas (marqués `source_test = "supplement"`) → couverture
  36/36 ; retirer une entrée désactive le complément.
- Régénérer : `.venv/bin/python scripts/calibration_build.py --report`.
  L'extraction lit les exports `documentation-*` (JSON), le canvas (HTML
  Mistral) ou `calibration/sources/<bot>.md` ; le rapport liste les capacités
  non couvertes.
- Fiche d'envoi (messages à poster dans l'ordre + pièces jointes) :
  `.venv/bin/python scripts/calibration_build.py --print <bot>`.
  Médias : `.venv/bin/python scripts/make_media.py --bot <bot>`.
- File d'envoi : `--write-queue` (écrit `calibration_queue.json` dans le run dir) ;
  envoi resumable : `scripts/etalon_run.py --bot <bot> --run-dir … --queue …`.
- **Audit d'intégrité** : `.venv/bin/python scripts/audit_etalon.py`
  (file vs export, tours manquants par conversation).
- **Complément** : `.venv/bin/python scripts/calibration_build.py --complement <bot>`
  écrit `calibration_queue_supp.json` (tours manquants) → à envoyer dans une
  **2e conversation** ; l'union des conversations d'une plateforme couvre le
  manifeste (`scripts/etalons.json` accepte plusieurs entrées par plateforme).
- Envoi (une seule fois) : sous-agent browser-use par bot, puis on gèle le
  HTML/JSON de la conversation obtenue.
- **Fixtures gelées** : `.venv/bin/python scripts/freeze_etalon.py [--clean]`
  copie l'artefact brut (payload `raw/` pour les plateformes API, HTML pour les
  DOM) + JSON dans `tests/fixtures/etalons/` ; `tests/test_etalon_fixtures.py`
  reparse hors navigateur (payload API via `extra={"api": …}`) et compare
  (détecte une régression de parseur).

## Pièges connus

- DOM des plateformes volatil → chaînes de sélecteurs de repli ; mettre à jour
  `message_selectors` + parser en cas de rupture.
- Cloudflare : Claude/Perplexity/Mistral forcent `engine: botasaurus` ; login et
  export doivent utiliser **le même moteur** (cookies liés à l'UA).
- Perplexity gratuit rate-limite → espacer (`--daily`), pas de scraping massif.
- Perplexity : lecture par API REST (`GET /rest/thread/<id>`, pagination par
  curseur) + GraphQL (`POST /rest/perplexity_ask/graphql`, découverte
  `LibraryRecentThreadsPaginationQuery`) ; les cookies sont mintés une fois via
  le navigateur (`profiles/perplexity`, botasaurus) puis tout passe en HTTP
  pur. Payload brut dans `exports/perplexity/raw/` ; repli DOM opt-in
  (`services.perplexity.dom: true`).
- Gemini : lecture par RPC `batchexecute`
  (`/_/BardChatUi/data/batchexecute` : `MaZiqc` liste, `hNvQHb` fil) avec les
  cookies du profil et les jetons tournants de la page (`SNlM0e`, `F.sid`,
  `bl`) ; 429 et erreur applicative `BardErrorInfo` 1096 gérées par backoff.
  Mode API **opt-in** (`services.gemini.api: true`), payload brut dans
  `exports/gemini/raw/`.
- **Fils « collés en bas »** (Perplexity, parfois Claude/ChatGPT) : un `scrollTop`
  programmatique est annulé. Utiliser `session.scroll_wheel` (molette réelle CDP)
  pour remonter le conteneur, pas `scrollTop`.
- ChatGPT (UI « DIL » 2026) : plus de `data-message-author-role`/`data-message-id` ;
  tours `[data-turn-key]`, messages `[data-chatgpt-search-unit-key]`, historique
  chargé à la molette réelle (accumulateur JS). Limites plateforme : images markdown
  distantes rendues en placeholder sans URL (perdues), canvas plus exposé dans la
  page (`artifacts` vide), modèle absent du DOM (`null`). Timestamps de
  conversation : script bootstrap `create_time`/`update_time` (retiré à
  l'hydratation → repli fetch SSR).
- ChatGPT (API) : Cloudflare challenge les clients HTTP bruts sur
  `/backend-api/*` (`Cf-Mitigated: challenge`, captcha géré) — urllib reçoit un
  403 HTML même avec cookies + Client Hints. Les lectures passent par le réseau
  du navigateur (`_ChatGPTHttpClient` → `session.api_request`, contexte
  Chromium) ; repli urllib si le moteur ne l'expose pas (botasaurus).
- Conversations **indisponibles** (supprimées/privées) : l'URL redirige vers
  l'accueil ; elles sont marquées `unavailable` dans `conversation_list.json` et
  plus retentées. `--retry-unavailable` lève le flag pour un nouvel essai.
- Une **fusion JSON partielle** (scrape tronqué) ne remplace pas l'archive :
  statut `kept` (préfixe/suffixe) → ne pas dégrader un export complet.
- Mistral : application unique `/work` (l'ancien `/chat` a fusionné dedans) ;
  les URLs `/chat/<id>` sont réécrites en `/work/<id>`. Lecture par API tRPC
  (`/api/trpc/…` : `chat.last`, `message.all,chat.byId,canva.all`) avec
  `cookies/mistral.json` (à rafraîchir via `run.py --login mistral` quand la
  session expire) ; `services.mistral.api: false` force le DOM. Timestamps non
  exposés (`null`).
- Claude : lecture par API REST
  (`/api/organizations/<org>/chat_conversations_v2` puis
  `/api/organizations/<org>/chat_conversations/<id>?tree=True`) avec les cookies
  du profil (`cookies/claude.json` sinon mint via navigateur, UA lié au profil) ;
  `services.claude.api: false` force le DOM. Payload brut dans
  `exports/claude/raw/`.
- Grok : pas de navigateur → pas de screenshots, pas de HTML ; payload brut dans
  `exports/grok/raw/`. Les pièces jointes sont téléchargées via un `fetch`
  authentifié par cookies.
- ChatGPT, Claude, Mistral, Perplexity et Gemini (opt-in) en mode API : pas de
  HTML ni de screenshots, payload brut dans `exports/<bot>/raw/` ;
  `--variants` exige le mode DOM pour ChatGPT.
- Après un `reparse_exports.py` ou un `download_images.py`, relancer
  `rag_index.py --force` (cache par empreinte de message).

## Conventions

- Commentaires/docstrings en **français**, code et noms en anglais ; type hints.
- Pas de dépendance nouvelle sans la déclarer dans `requirements.txt`.
- Commits : `type(scope): résumé` en français, minuscule (ex.
  `fix(gemini): listes imbriquées`, `feat(web): ...`). Ne pas commit sans demande.
