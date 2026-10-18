# AI Conversations in JSON

[![CI](https://github.com/Antonio-Faure/AI-conversations-in-json/actions/workflows/ci.yml/badge.svg)](https://github.com/Antonio-Faure/AI-conversations-in-json/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

Export automatisé des conversations IA dans un **format JSON standardisé** +
l'**artefact brut** de chaque conversation (payload API brut pour les
plateformes API — ChatGPT, Claude, Gemini, Grok, Mistral, Perplexity — HTML de
la page pour le repli DOM).
Plateformes actives : **ChatGPT**, **Claude**, **Gemini**, **Perplexity**,
**Grok** (xAI) et **Mistral**.

Chaque conversation produit :

```
exports/<platform>/<nom de la conversation>.json      # messages standardisés
exports/<platform>/<nom de la conversation>.html      # page complète (brut, DOM)
exports/<platform>/raw/<nom de la conversation>.json  # payload API brut (API)
```

Plus un inventaire par chatbot :

```
exports/<platform>/conversation_list.json
```

## Fonctionnement

```
run.py (CLI)
  └── Orchestrator (src/orchestrator.py)
        └── registry des services (chatgpt, claude, gemini, perplexity, grok, mistral)
              1. ouvre la home -> sidebar -> liste des conversations
              2. sélectionne les cibles selon le mode (daily / monthly)
              3. pour chacune : navigation + scroll -> HTML complet (DOM) ou
                 appels API -> payload brut (ChatGPT, Claude, Gemini, Grok,
                 Mistral, Perplexity)
              4. parsing -> Conversation standardisée (+ code_blocks)
              5. écriture atomique de <nom>.json et de l'artefact brut
                 (raw/<nom>.json pour l'API, .html pour le DOM)
              6. mise à jour de conversation_list.json
```

- **Écriture JSON intelligente** : si le contenu est identique, le fichier
  n'est **pas** réécrit ; si de nouveaux messages suivent, ils sont **patchés**
  (ajoutés sans écraser l'existant) ; sinon réécriture complète. L'artefact
  brut (HTML ou payload API) est toujours écrasé (aucune donnée perdue).
- **Pas de logs sur disque, pas de dossiers par session** : seul l'export final
  est conservé (console uniquement).
- **Profils persistants** : cookies de connexion dans `profiles/<service>/`.
- **Scroll infini** : sidebar et fil de discussion scrollés jusqu'à stabilisation.
- **Anti-Cloudflare** : moteur Playwright par défaut, bascule Botasaurus
  automatique si un challenge est détecté.

## Installation

```bash
python3 -m venv .venv
# torch CPU (evite le wheel CUDA lourd) puis le reste
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # binaire navigateur
```

Le modèle d'embeddings `google/embeddinggemma-2` (~1,5 Go, multimodal
texte/images/audio/vidéo) est téléchargé automatiquement au premier usage du
RAG.

## Première connexion (obligatoire, une fois par service)

```bash
.venv/bin/python run.py --login chatgpt
.venv/bin/python run.py --login claude
```

Un navigateur s'ouvre : connectez-vous, le script détecte la fin de la
connexion et ferme tout seul. Les cookies sont sauvegardés dans
`profiles/<service>/` et réutilisés par les exports headless.

Le login accepte aussi `gemini`, `perplexity`, `grok` et `mistral`.
Pour Grok et Mistral (pas de profil scraper dédié), la commande **capture les
cookies** : elle ouvre un navigateur visible, détecte la connexion
automatiquement (cookie de session) et écrit `cookies/<service>.json`.
Grok utilise Firefox natif (Chromium est bloqué par Cloudflare), Mistral le
moteur botasaurus. Vérification : `scripts/check_cookies.py`. En mode API,
Mistral lit `cookies/mistral.json` directement : relancer
`run.py --login mistral` quand la session expire (les lectures HTTP n'ouvrent
pas de navigateur). Perplexity réutilise les cookies du profil
(`profiles/perplexity`, mintés au début de chaque run) : relancer
`run.py --login perplexity` si la session expire. Gemini en mode API lit aussi
les cookies du profil (`profiles/gemini`) et les jetons de la page :
`run.py --login gemini` en cas de session expirée.

Important : le login utilise **le même moteur que l'export** du service
(Botasaurus pour Claude et Perplexity). Les cookies anti-bot comme
`cf_clearance` sont liés à l'User-Agent — une connexion Playwright ne serait
pas réutilisable par Botasaurus, et inversement.

## Utilisation

```bash
# routine quotidienne : inconnues + 20 dernières conversations (tous les services)
.venv/bin/python run.py --daily

# rafraîchissement complet : liste TOUTES les conversations et les scrape
.venv/bin/python run.py --monthly     # --full est un alias

# ne scraper qu'un service (option répétable) : gain de temps
.venv/bin/python run.py --daily --service claude
.venv/bin/python run.py --monthly -s gemini -s perplexity

# debug : navigateur visible, 5 conversations max, logs verbeux
.venv/bin/python run.py --daily --service chatgpt --headful --limit 5 --verbose

# capturer aussi les versions alternatives (editions, regenerations)
# (implemente pour mistral et chatgpt)
.venv/bin/python run.py --monthly --service mistral --variants
.venv/bin/python run.py --monthly --service chatgpt --variants
```

Codes de sortie : `0` OK, `1` au moins un échec, `2` erreur d'usage/config.

### Modes

| Mode | Découverte | Scrape |
|---|---|---|
| `--daily` | liste les conversations | **inconnues** (jamais scrapées) ∪ **20 plus récentes** |
| `--monthly` / `--full` | liste TOUTES les conversations | **toutes** (rafraîchissement complet) |

L'artefact brut manquant (HTML ou payload API, ex. après migration) est produit
dès qu'une conversation repasse en `daily` (si elle fait partie des 20 récentes)
ou en `monthly`.

## Format JSON d'une conversation

`exports/<platform>/<nom>.json` :

```json
{
  "conversation_id": "abc123",
  "platform": "chatgpt",
  "title": "Titre de la conversation",
  "url": "https://chatgpt.com/c/abc123",
  "model": "gpt-4",
  "started_at": "2025-01-15T10:30:00Z",
  "last_message_at": "2025-01-15T11:45:00Z",
  "exported_at": "2026-09-10T00:00:00Z",
  "messages": [
    {
      "conversation_id": "abc123",
      "message_id": "uuid-du-message",
      "role": "user",
      "platform": "chatgpt",
      "model": null,
      "timestamp": "2025-01-15T10:30:00Z",
      "texte": "Texte complet du message (markdown, fences de code incluses)",
      "code_blocks": []
    },
    {
      "conversation_id": "abc123",
      "message_id": "uuid-reponse",
      "role": "assistant",
      "platform": "chatgpt",
      "model": "gpt-4",
      "timestamp": "2025-01-15T10:30:05Z",
      "texte": "Voici un exemple :\n```python\nprint(1)\n```",
      "code_blocks": [
        {"language": "python", "code": "print(1)"}
      ]
    }
  ]
}
```

- `role` ∈ `user | assistant | system | tool`
- `texte` : markdown complet (le code en fences y figure **aussi**) ;
  `code_blocks` en est la vue structurée `{language, code}`.
- timestamps ISO-8601 UTC (`...Z`) ou `null` quand la plateforme ne les expose pas.
- Le fichier `.html` contient le DOM complet de la page de conversation
  (repli DOM). Les plateformes API (ChatGPT, Claude, Gemini, Grok, Mistral,
  Perplexity) n'écrivent plus de HTML : le payload brut `raw/<nom>.json` le
  remplace comme artefact de re-mapping (JSON-only). Le mode API est le défaut
  (`services.<bot>.api: true`) ; `api: false` force le repli DOM, qui réécrit
  le HTML (`services.perplexity.dom: true` pour Perplexity ; Gemini est
  opt-in : `services.gemini.api: true`, actif dans `config.yaml`).
- `variants` (optionnel, capture avec `--variants`) : versions alternatives du
  message — éditions d'un message utilisateur (`kind: "edit"`) ou réponses
  régénérées (`kind: "regeneration"`) — dans l'ordre de l'interface ; `texte`
  reste la version active, chaque variante porte `active`. Plateformes : mistral
  (switcher du fil) et chatgpt (dialogue « Versions »).

## conversation_list.json

Un inventaire par dossier chatbot, régénéré à chaque run :

```json
{
  "platform": "chatgpt",
  "updated_at": "2026-09-10T00:00:00Z",
  "count": 168,
  "conversations": [
    {
      "conversation_id": "abc123",
      "title": "Titre de la conversation",
      "url": "https://chatgpt.com/c/abc123",
      "message_count": 12,
      "last_message_at": "2025-01-15T11:45:00Z",
      "has_code": true,
      "scraped": true,
      "file": "titre-de-la-conversation",
      "scraped_at": "2026-09-10T00:00:00Z"
    }
  ]
}
```

Les conversations découvertes mais pas encore scrapées ont `scraped: false`
et des champs `message_count`/`last_message_at`/`has_code` à `null`.

## Moteurs navigateur

Deux moteurs partagent la même façade ; les services ne voient pas la différence :

| moteur | rôle |
|---|---|
| `playwright` | Chromium piloté par Playwright (par défaut) |
| `botasaurus` | Chrome patché + `bypass_cloudflare` (contourne Cloudflare/Turnstile) |

```yaml
engine: auto        # auto = Playwright, bascule Botasaurus si challenge détecté
```

En `auto`, un `BlockedError` relance automatiquement le service avec Botasaurus
(une fois) — à la découverte comme sur une conversation. `enable_xvfb: true`
lance Chrome headful dans un affichage virtuel (paquet système `xvfb`).

## Parallélisme (profils isolés)

Scraping simultané de plusieurs chatbots (domaines différents) :

```bash
.venv/bin/python run.py --monthly -s claude -s mistral --parallel 2
.venv/bin/python run.py --daily   -s grok -s chatgpt --parallel 2   # défaut via config
```

- **Profils isolés** : chaque service a son profil persistant
  `profiles/<service>/` (cookies, localStorage, cache). Aucun partage.
- **Fingerprints randomisés et stables par profil** : User-Agent, taille de
  fenêtre, langue et `hardwareConcurrency`/`deviceMemory` sont dérivés du nom du
  service et **persistés** dans `profiles/<service>/fingerprint.json` (stables
  entre les runs pour ne pas invalider `cf_clearance`, différents d'un chatbot à
  l'autre).
- **Parallélisme par domaine uniquement** : les services partageant un même
  domaine sont regroupés et exécutés **séquentiellement** ; seuls les groupes de
  domaines différents tournent en parallèle (`--parallel N`).
- **Séquentiel dans chaque tête** : aucune concurrence sur un même chatbot.
- **Rythmes indépendants** : `pacing_ms` est jitté par service (`_jitter_pacing`),
  donc deux têtes ne frappent pas au même rythme.
- Les cookies restent par service (`cookies/<service>.json` +
  `profiles/<service>/`), jamais partagés.

## Migration des anciens exports

Un script convertit l'ancien format (`exports/<date>/chatgpt/<id>.json` +
`.state/state.json`) vers le nouveau et génère `conversation_list.json` :

```bash
.venv/bin/python scripts/migrate_exports.py
```

Les anciens fichiers ne sont pas supprimés (les retirer après vérification).
L'artefact brut n'existant pas dans l'ancien schéma, il est produit au prochain
run (HTML ou payload API selon la plateforme).

## Audit

```bash
.venv/bin/python scripts/audit_exports.py exports   # liens, fences, artefacts DOM
```

## Qualité des données (nettoyage)

Correctifs appliqués aux parseurs et à la régénération :

- **Alternance garantie** : jamais deux messages `user` ou deux `assistant`
  consécutifs — `normalize_messages` (src/schema.py) fusionne les tours éclatés
  et supprime les doublons, pour les 6 chatbots.
- **LaTeX préservé** : KaTeX (`annotation x-tex`), Gemini `data-math` et
  ChatGPT `data-math-source` sont convertis en `\(...\)` / `\[...\]` / `$$...$$`
  au lieu d'être perdus.
- **Indentation du code** préservée (les fences ne sont plus `strip`ées) ;
  la langue des blocs Mistral est récupérée depuis l'en-tête.
- **Grok nettoyé** : suppression des balises `<grok:render>`, `<argument>`,
  `xaiArtifact`, `<br>` (`src/utils/cleanup.py`).
- **Gemini** : « Vous avez dit » et la duplication des messages utilisateur
  supprimés (libellés lecteur d'écran exclus).
- **Claude** : labels de réflexion/statut de tour retirés, modèle lu depuis
  `model-selector-dropdown`.

- **URL de chaque conversation** : renseignée au crawl dans le JSON + l'inventaire
  (reconstruite par plateforme, ou lue depuis le `<link rel="canonical">` du HTML
  pour Mistral → distingue `/chat` de `/work`). Backfill sur l'existant :
  `.venv/bin/python scripts/backfill_urls.py`.

Régénération **sans re-crawler** depuis les artefacts bruts sauvegardés :
payload API brut (`exports/<platform>/raw/`) pour ChatGPT, Claude, Gemini,
Grok, Mistral et Perplexity, HTML du repli DOM.

```bash
.venv/bin/python scripts/reparse_exports.py                 # toutes plateformes
.venv/bin/python scripts/reparse_exports.py --platform gemini --no-backup
```

Les métadonnées absentes de l'artefact (timestamps/modele) sont fusionnées
depuis l'ancien JSON (`.json.bak` conservé). Pour les plateformes migrées en
API, le payload brut prime sur un `.html` résiduel (migration JSON-only). Une
conversation sans HTML ni payload (export ancien, plateforme migrée) est
nettoyée directement. Re-lancer ensuite `scripts/rag_index.py --force` (les
vecteurs inchangés sont repris du cache).

## RAG (recherche sémantique des messages)

Indexation **un vecteur par message** et recherche par **similarité cosinus**.

- **Embeddings locaux** : `google/embeddinggemma-2` (768 dim, MRL 512/256/128,
  prompts de tâche, multimodal) — offline/gratuit, vecteurs normalisés ;
  `rag_model`/`rag_dim` restent configurables (0 = dimension déduite du modèle).
- **Stockage SQLite + sqlite-vec** : `rag/messages-eg2.db`
  (`messages` = texte + métadonnées, `vec_messages` = vecteurs `distance_metric=cosine`,
  `media`/`vec_media` = médias).
- **Granularité message**, aucune déduplication, aucun résumé.
- Indexation **incrémentale** : une conversation dont `exported_at` n'a pas changé
  est ignorée (sauf `--force`).
- **Cache de vecteurs par message** : chaque message porte une empreinte
  `sha256(modele+texte)`. Avant de calculer un embedding, le vecteur est
  recherché dans SQLite ; s'il existe il est réutilisé, sinon calculé puis
  stocké. Après un patch, seuls les **nouveaux** messages sont recalculés.

```bash
# indexer (tous les services)
.venv/bin/python scripts/rag_index.py

# indexer un service / forcer / limiter (debug)
.venv/bin/python scripts/rag_index.py --platform grok
.venv/bin/python scripts/rag_index.py --force
.venv/bin/python scripts/rag_index.py --limit 10

# rechercher (top-k cosinus, filtres optionnels)
.venv/bin/python scripts/rag_search.py "améliorer mes vidéos YouTube" -k 10
.venv/bin/python scripts/rag_search.py "kubernetes" --platform gemini --role assistant
```

Chaque résultat affiche `cos=<similarité> [<platform>/<role>] <timestamp> conv=<id>`
puis un extrait du message.

### Médias (images, audio, vidéo) + recherche multimodale

La passe médias encode **1 vecteur par fichier** (images `images/`, audio/vidéo
`files/`) dans l'espace multimodal d'EmbeddingGemma 2, avec les métadonnées
(chemin, type, plateforme, conversation/message d'origine, texte de référence) ;
le budget vision par image est réglable (`--image-budget 70/140/280/560/1120`).
Les médias sans conversation (fichiers non référencés) sont élagués par
`scripts/prune_media.py` (relie les copies d'un média lié, supprime le reste).

```bash
# médias -> table vec_media (incrémental : empreinte par fichier)
.venv/bin/python scripts/rag_index_media.py

# recherche texte -> médias (même espace vectoriel que les messages)
.venv/bin/python scripts/rag_index_media.py --search "un paysage" --kind image
```

La page web expose la recherche média dans `/api/search` : les médias
pertinents sont **rattachés à leur conversation** (un média compte comme un
hit, il participe au score du groupe) et affichés en vignettes/lecteurs.

## Serveur MCP (recherche depuis un agent)

`scripts/mcp_conversations.py` expose le RAG et les exports en **serveur MCP
stdio** (SDK officiel `mcp` v2), enregistré dans la config globale opencode sous
`mcp.servers.aicv` : les agents cherchent dans l'archive avec des outils natifs.

| Outil | Rôle |
| --- | --- |
| `search` | recherche hybride (embeddings + mots-clés), groupée par conversation |
| `grep` | chaîne exacte ou regex dans les exports JSON (sans modèle) |
| `get_conversation` / `get_message` | JSON standardisé complet / message précis |
| `list_conversations` | inventaire `conversation_list.json` |
| `stats` | compteurs par plateforme + fraîcheur de l'index |

```bash
# lancer le serveur à la main (stdio : stdout = JSON-RPC, logs sur stderr)
.venv/bin/python scripts/mcp_conversations.py
.venv/bin/python scripts/mcp_conversations.py -c /autre/chemin/config.yaml

# enregistrer dans opencode (config globale)
opencode mcp add aicv --global -- .venv/bin/python scripts/mcp_conversations.py
```

Le modèle d'embeddings n'est chargé qu'au premier `search` ; `stats()` indique la
date d'indexation (`rag_index.py` après un re-parse). La skill
`.opencode/skills/conversations-search/` documente l'usage côté agent.

## Page web de recherche

Interface locale (dark theme, responsive, Crimson Pro + DM Sans) pour explorer
les conversations des 6 chatbots.

```bash
.venv/bin/python scripts/serve_web.py          # ouvre Chrome sur http://127.0.0.1:8765
.venv/bin/python scripts/serve_web.py --host 0.0.0.0 --port 9000 --no-open
```

Au démarrage, le serveur **ouvre le navigateur** (Chrome si installé, sinon le
navigateur par défaut) sur la bonne page (`--no-open` pour désactiver).

- **Double recherche combinée** : mots-clés exacts (LIKE + bonus phrase) **et**
  similarité cosinus (EmbeddingGemma + sqlite-vec), exécutées ensemble puis fusionnées
  par score : `score = alpha*sémantique + (1-alpha)*mots_clés` (curseur `alpha`).
- **Filtres** : par chatbot (chips), par modèle, par plage de dates.
- **Résultats groupés par conversation** : les conversations ayant **plusieurs**
  messages pertinents sont affichées en premier (regroupées), les messages
  isolés ensuite (probablement moins pertinents).
- **Sans recherche** : la page présente les **dernières conversations**.
- **Accueil (tableau de bord)** : heatmap d'activité (365 jours, clic sur un jour
  → vue « journée »), conversations par plateforme, top modèles, « Au hasard »,
  « Il y a un an aujourd'hui ». Les panneaux **Conversations par plateforme** et
  **Top modèles** tournent chacun toutes les 5 minutes sur leur dimension : vue
  d'ensemble, puis une série mensuelle par élément (flèches et pastilles pour
  naviguer à la main).
- **Lien cliquable** `Ouvrir ↗` vers la conversation d'origine (site d'origine),
  dans les résultats et dans la vue conversation.
- **Rendu riche** : markdown (gras, listes, tableaux, liens `target=_blank`),
  **LaTeX via KaTeX**, blocs de code séparés avec bouton copier
  (`marked` + `DOMPurify` + `KaTeX`, CDN).
- **Versions alternatives** : un message à plusieurs versions (éditions ou
  régénérations) porte un badge `N versions` et un bloc dépliable
  `Versions alternatives` (version active marquée, `kind` affiché).
- **Clic sur un message** → ouvre la conversation complète, positionnée et
  surlignée sur ce message ; bouton retour.
- **🎲 Aléatoire** : ouvre une conversation au hasard (pour le jeu de correction).
- **Sidebar** : conversations des résultats et, en vue conversation, ancres vers
  chaque message. Barre de recherche **sticky et repliable** (`⚙️ Paramètres`).

L'API locale (même serveur) : `/api/search`, `/api/conversation`,
`/api/conversations`, `/api/random`, `/api/filters`, `/api/stats`,
`/api/activity`, `/api/timeline`, `/api/day`. Le modèle
d'embeddings se charge au premier appel (quelques secondes).

## Optimisations écriture & compute

| règle | mise en œuvre |
|---|---|
| JSON identique → pas d'écriture | `merge_conversation_json` compare hors `exported_at` |
| Nouveaux messages → patch | si les anciens messages sont un préfixe, seuls les nouveaux sont ajoutés |
| Artefact brut (HTML ou payload API) | toujours écrasé (rendu/données brutes) |
| Embeddings déjà calculés | cache par empreinte de message (`get_cached_vectors`) |
| Pas de surcharge CPU | torch limité à `cpu-1` threads (`AICV_TORCH_THREADS` pour forcer) |

Le résumé CLI distingue `ecrites=` (écrites/patchées), `patch=`, `inchanges=`
(non réécrites). L'indexation RAG affiche `vecteurs : N reutilises, M recalcules`.

## Configuration

Tout est dans [`config.yaml`](config.yaml) : chemins **relatifs à la racine du
dépôt** (ou absolus), services (les 6 `enabled: true`), headless, timeouts,
scroll, `parallel`, `cookies_dir`, `rag_db`/`rag_model`. Claude,
Perplexity et Mistral forcent `engine: botasaurus` (Cloudflare bloque
Playwright) ; Grok utilise l'API HTTP + cookies (aucun navigateur) ; ChatGPT,
Claude, Mistral et Perplexity lisent par défaut via l'API
(`services.<bot>.api: true` : payload brut archivé au lieu du HTML) et gardent
le DOM en repli (`api: false`, ou `services.perplexity.dom: true`). Gemini est
en API **opt-in** (`services.gemini.api: true`, actif dans `config.yaml`). Les
valeurs omises reprennent `DEFAULT_CONFIG` (`src/orchestrator.py`).

## Architecture

```
├── run.py                     # CLI (--daily / --monthly|--full / --service / --parallel / --login)
├── config.yaml                # configuration (chemins relatifs, services)
├── scripts/
│   ├── migrate_exports.py     # migration ancien format -> exports/<platform>/
│   ├── check_cookies.py       # vérifie l'auth par cookies (Grok, Mistral)
│   ├── capture_cookies.py     # capture les cookies (navigateur visible)
│   ├── rag_index.py           # indexe les messages (embeddings -> sqlite-vec)
│   ├── rag_index_media.py     # indexe les médias (images/audio/vidéo -> vec_media)
│   ├── rag_search.py          # recherche cosinus dans le RAG
│   ├── mcp_conversations.py   # serveur MCP stdio (search/grep/get/list/stats)
│   ├── reparse_exports.py     # régénère les JSON depuis les artefacts bruts (sans re-crawl)
│   ├── backfill_urls.py       # ajoute le champ url (JSON + inventaire)
│   ├── serve_web.py           # serveur local + API + ouverture navigateur
│   └── audit_exports.py       # audit qualité des JSON exportés
├── src/
│   ├── orchestrator.py        # workflow (modes, services, inventaire, JSON + artefact brut)
│   ├── browser.py             # moteur Playwright (profil persistant, scroll)
│   ├── browser_botasaurus.py  # moteur Botasaurus (anti-Cloudflare, même façade)
│   ├── http_client.py         # client HTTP + cookies (services sans navigateur)
│   ├── cookies.py             # capture cookies (browser / firefox)
│   ├── fingerprint.py         # fingerprints stables et isolés par profil
│   ├── selectors.py           # traduction sélecteurs Playwright -> CSS (botasaurus)
│   ├── schema.py              # schéma JSON standardisé + code_blocks
│   ├── services/              # chatgpt, claude, gemini, perplexity, grok, mistral
│   │   ├── base.py            # BaseService : découverte + scrape + parse
│   │   ├── grok.py            # API REST + cookies (uses_browser=False)
│   │   ├── claude.py          # API REST + cookies ; repli DOM (api: false)
│   │   ├── mistral.py         # API tRPC + cookies ; repli DOM ; /chat + /work
│   │   ├── perplexity.py      # API REST/GraphQL + cookies ; repli DOM (dom: true)
│   │   └── gemini.py          # API batchexecute (opt-in) ; DOM par défaut
│   ├── parsers/               # parsing DOM (BeautifulSoup, testable sans navigateur)
│   │   ├── base.py            # BaseParser : parse(), parse_links(), text_of()
│   │   ├── claude.py          # DOM + arbre API (variantes natives)
│   │   ├── mistral.py         # DOM + payload tRPC (variantes, canvas)
│   │   ├── perplexity.py      # DOM + payload /rest/thread (sources, étapes)
│   │   ├── gemini.py          # DOM + payload batchexecute (sources, fichiers)
│   │   └── grok.py            # construit depuis les données de l'API
│   ├── rag/                   # RAG : embeddings configurables + SQLite/sqlite-vec
│   │   ├── embeddings.py      #   Embedder (EmbeddingGemma 768, prompts de tâche)
│   │   ├── store.py           #   VectorStore (messages + vec_messages)
│   │   ├── indexer.py         #   indexation incrémentale par lots
│   │   ├── media.py           #   médias (images/audio/vidéo + vec_media)
│   │   └── search.py          #   recherche combinée (cosinus + mots-clés)
│   └── utils/
│       ├── raw_payload.py     # payload API brut (exports/<platform>/raw/)
│       ├── html_render.py     # HTML autonome (utilitaires/tests)
│       ├── cleanup.py         # nettoyage texte par plateforme (Grok...)
│       ├── logging.py         # logging console (pas de fichier)
│       └── file_utils.py      # exports atomiques, noms, conversation_list
├── web/                       # page de recherche (index.html, app.js, style.css)
├── cookies/                   # cookies d'auth (ignorés git)
├── rag/                       # base vectorielle messages-eg2.db (ignorée git)
├── profiles/                  # profils navigateurs (ignorés git)
└── exports/                   # sorties (ignorées git)
```

## Tests

Les parsers sont testés **sans navigateur** sur des fixtures HTML (DOM) et des
payloads API (`tests/fixtures/*_api_*.json`) ; l'orchestrateur avec des services
et sessions factices ; le CLI sans lancer Chromium.

```bash
.venv/bin/python -m pytest
```

## Dépannage

| Symptôme | Cause / solution |
|---|---|
| `session expirée` / `SKIP` | relancer `run.py --login <service>` |
| `bloque par un challenge anti-bot` | cookie `cf_clearance` expiré → `--login <service>` ; vérifier `enable_xvfb: true` |
| `aucun message reconnu` | DOM de la plateforme changé → mettre à jour `src/parsers/<platform>.py` |
| `Executable doesn't exist` | `.venv/bin/python -m playwright install chromium` |
| Export vide | vérifier `profiles/<platform>/` et lancer `--headful --verbose` |
| Perplexity : conversations vides en rafale | rate-limit du forfait gratuit → espacer les runs (`--daily`) |
| Grok / Mistral : `cookies absents` | lancer `run.py --login grok` (ou `mistral`) puis `scripts/check_cookies.py` |
| Claude/Mistral/Perplexity/Gemini API : `non authentifie` (401/403) | cookies du profil expirés → `run.py --login <service>` |
| Claude : `organisation introuvable` | session invalide → `run.py --login claude` |

## Limites connues

- ChatGPT : timestamps absents du DOM → récupérés depuis les props internes React
  (best effort) ; sinon `started_at`/`last_message_at` sont `null`.
- Grok : Cloudflare bloque le Chromium automatisé → scraping via l'**API REST +
  cookies** (`uses_browser=False`) ; aucun HTML n'est généré, le payload brut
  de l'API est conservé dans `exports/grok/raw/`. Modèle : `grok-3`.
- Claude : lecture par l'**API REST** (`/api/organizations/<org>/chat_conversations*`,
  cookies du profil) ; aucun HTML ni screenshot, payload brut dans
  `exports/claude/raw/`. `services.claude.api: false` force le DOM.
- Mistral : application unique `/work` (l'ancien `/chat` a fusionné dedans) ;
  les URLs `/chat/<id>` sont réécrites en `/work/<id>`. Lecture par l'**API
  tRPC** (`/api/trpc/…`, cookies `cookies/mistral.json` — à rafraîchir via
  `run.py --login mistral` quand la session expire) ; payload brut dans
  `exports/mistral/raw/`, `services.mistral.api: false` force le DOM.
  Timestamps non exposés (`null`).
- Perplexity : lecture par l'**API REST** (`GET /rest/thread/<id>`, pagination
  par curseur) + GraphQL (`POST /rest/perplexity_ask/graphql` pour la
  découverte) ; les cookies sont mintés une fois via le navigateur
  (`profiles/perplexity`, botasaurus) puis tout passe en HTTP pur. Payload brut
  dans `exports/perplexity/raw/`, repli DOM via `services.perplexity.dom: true`.
  Le forfait **gratuit** rate-limite la lecture en rafale (redirection vers
  l'accueil → conversations vides). `--daily` les reprend progressivement ; le
  modèle vient de l'API de découverte (`displayModel.modelID`).
- Gemini : lecture par RPC `batchexecute` (`MaZiqc` liste, `hNvQHb` fil) avec
  les cookies du profil et les jetons tournants de la page ; mode API
  **opt-in** (`services.gemini.api: true`), payload brut dans
  `exports/gemini/raw/`, sans HTML ni screenshots. Les 429 et l'erreur
  applicative `BardErrorInfo` 1096 sont absorbés par backoff (pacing 3 s) ; en
  cas de limite persistante le service s'arrête proprement et `--daily` reprend
  les conversations sans `raw/`.
- Le nom de fichier vient du titre (slugifié) : une collision de titres est
  désambiguïsée par un suffixe d'id. Un changement de titre conserve le fichier
  existant (le nom est mémorisé dans `conversation_list.json`).
- Les DOM des plateformes changent souvent : les parsers ont des chaînes de
  fallback, mais une rupture DOM demande une mise à jour des sélecteurs.

## Étalons locaux

Les ids des conversations d'étalonnage ne sont pas versionnés : copier
[`scripts/etalons.example.json`](scripts/etalons.example.json) vers
`scripts/etalons.json` et y renseigner ses propres ids. Les fixtures de
`tests/fixtures/etalons/` sont **anonymisées** ; `scripts/sanitize.py`
(`--check` / `--apply`) garantit qu'aucune trace personnelle n'est publiée.

## Licence et usage

Distribué sous licence [MIT](LICENSE).

Usage prévu : archiver **ses propres** conversations, sur **son** compte, pour
un usage personnel (recherche, RAG local). Le scraping automatisé peut être
encadré par les conditions d'utilisation des plateformes : vérifiez-les et
restez mesuré (espacement des requêtes, aucune donnée de tiers, aucun
contournement de paywall).

