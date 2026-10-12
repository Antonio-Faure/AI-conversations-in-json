---
name: conversations-search
description: Recherche dans les conversations IA archivees (chatgpt, claude, gemini, perplexity, grok, mistral) via le serveur MCP `aicv` (search/grep/get_conversation/get_message/list_conversations/stats) ou en repli les scripts RAG. Use when looking up what was said in past AI conversations, searching the local conversation archive, finding a conversation/message by text or URL, or checking RAG index freshness.
---

# Recherche dans les conversations archivees

Deux couches de donnees, deux outils :

| Donnee | Contenu | Outil |
| --- | --- | --- |
| **Index RAG** (`rag/messages.db`) | un vecteur BGE-M3 par message (texte + raisonnement + sources + outils) | `aicv.search` |
| **Exports JSON** (`exports/<platform>/*.json`) | JSON standardise complet + `conversation_list.json` | `aicv.grep`, `aicv.get_conversation`, `aicv.get_message`, `aicv.list_conversations` |

Plateformes : `chatgpt`, `claude`, `gemini`, `perplexity`, `grok`, `mistral`.

## Outils MCP (`aicv`)

En Code Mode opencode : `tools.aicv.search(...)`, `tools.aicv.grep(...)`, etc.
(Sinon outils natifs `aicv_search`, `aicv_grep`, ….)

- **`search(query, k=10, platforms=None, alpha=0.6, date_from=None, date_to=None, model=None)`**
  recherche hybride (embeddings + mots-cles), resultats **groupes par
  conversation** (titre, url, id, messages matches avec extraits et scores
  `score`/`semantic_score`/`keyword_score`).
  - question en langage naturel, idee vague, sujet → `search`.
  - `alpha` : 0 = mots-cles seuls, 1 = embeddings seuls (0.6 par defaut).
  - dates ISO ; une date seule (`2026-06-08`) couvre la journee entiere.
  - `k` borne a 100.
- **`grep(pattern, platform=None, regex=false, limit=50)`** : chaine **exacte**
  (ou regex) dans les exports, sans modele — rapide (~1 s sur tout le corpus).
  - URL, identifiant, nom de fichier, extrait de code, expression precise.
  - insensible a la casse ; cherche dans texte, raisonnement, sources, outils,
    artefacts, variantes, titres/URLs ; inclut les conversations non scrapees
    (`conversation_list.json`, champ `list.*`).
- **`get_conversation(platform, conversation_id, max_messages=0)`** : JSON
  standardise complet ; `max_messages=N` = les N premiers messages (+
  `total_messages`, `truncated`). `conversation_id` accepte un **prefixe** non
  ambigu.
- **`get_message(platform, conversation_id, message_id_or_index)`** : un message
  par index 1-based (`"3"`) ou `message_id` (ou prefixe). Renvoie `texte` +
  extras (`reasoning`, `sources`, `tools`, `artifacts`, `variants`).
- **`list_conversations(platform=None, limit=20, sort="recent")`** : inventaire
  `conversation_list.json` (`recent` | `title` | `messages` ; `limit=0` = tout).
  Les entrees `scraped: false` / `unavailable: true` n'ont pas de fichier.
- **`stats()`** : compteurs par plateforme (messages/conversations indexes,
  date d'indexation, conversations listees/scrapees/indisponibles). **A
  regarder avant de conclure qu'une recherche ne donne rien.**

## Methode

1. `search` d'abord (langage naturel). Si le resultat est vide ou douteux :
   `stats()` pour verifier la fraicheur de l'index et la couverture.
2. Chaine exacte/URL → `grep`.
3. Identifier une conversation/message précis → `get_conversation` /
   `get_message` (utiliser `index` ou `message_id` remontes par `search`/`grep`).
4. Pour explorer un theme sans requete precise → `list_conversations`.

## Limites / pieges

- **L'index RAG ne contient que les messages**, pas les payloads bruts
  (`exports/<platform>/raw/`) ni le HTML : une chaine presente uniquement dans
  un artefact brut ne ressort pas. Pour ca, grep direct dans les fichiers.
- **L'index n'est pas mis a jour automatiquement** : apres un scrape, un
  `reparse_exports.py` ou un `download_images.py`, relancer
  `.venv/bin/python scripts/rag_index.py` (incremental ; `--force` si besoin)
  avant de conclure a une absence. `stats()` donne `indexed_at`.
- `search` charge le modele BGE-M3 au **premier appel** (quelques secondes) :
  c'est normal ; les appels suivants sont rapides.
- Les snippets sont tronques (`snippet`) : le texte complet est dans
  `get_message` / `get_conversation`.
- Un `search` vide sur un sujet recent peut signifier « conversation pas encore
  scrapee » : `list_conversations` et `grep` couvrent ce cas.

## Repli CLI (sans MCP)

```bash
.venv/bin/python scripts/rag_search.py "requete" -k 10 --platform grok --role assistant
.venv/bin/python scripts/rag_index.py [--platform grok] [--force]   # mise a jour de l'index
.venv/bin/python scripts/rag_index.py --force                        # reindexation complete
```

- `rag_search.py` = cosinus pur (pas de fusion mots-cles ni de regroupement par
  conversation) ; pour l'equivalent complet, passer par le serveur MCP ou
  `scripts/serve_web.py` (page web `http://127.0.0.1:8765`).
- `rag_search.py` ne prend qu'une plateforme et un role, pas de dates.
- Grep en shell : `rg -i "motif" exports/*/*.json` (les fichiers `*.json.bak`
  sont des sauvegardes, les ignorer).

## Configuration

- Serveur : `scripts/mcp_conversations.py`, enregistre dans la config globale
  opencode (`mcp.servers.aicv`, `type: local`, commande =
  `<AICV_ROOT>/.venv/bin/python <AICV_ROOT>/scripts/mcp_conversations.py`).
- Config du serveur : `-c/--config` (defaut : `config.yaml` a la racine du
  depot). Les chemins relatifs du fichier de config sont resolus depuis **son**
  dossier ; un worktree peut donc pointer `-c ${AICV_ROOT}/config.yaml` pour
  utiliser les exports et l'index partages.
- Logs du serveur sur **stderr** uniquement (stdout = protocole JSON-RPC).
- Test rapide : `.venv/bin/python scripts/mcp_conversations.py -c ${AICV_ROOT}/config.yaml`
  puis taper un `initialize` JSON-RPC, ou lancer
  `pytest tests/test_mcp_conversations.py`.
