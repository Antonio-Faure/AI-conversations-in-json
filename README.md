# AI Conversations in JSON

Export automatisé (quotidien) de vos conversations IA — **ChatGPT, Claude,
Gemini, Perplexity** — dans un **format JSON standardisé**, via **Playwright**
ou **Botasaurus** (contournement Cloudflare).

Chaque plateforme a son propre scraper (navigation, scroll infini) et son
propre parser DOM, mais toutes convergent vers le même schéma de sortie.

## Fonctionnement

```
run.py (CLI)
  └── Orchestrator (src/orchestrator.py)
        └── par service : BrowserSession (profil persistant, cookies)
              1. ouvre la home -> sidebar -> liste des conversations (parser)
              2. pour chaque conversation : navigation + scroll infini
              3. extraction HTML (+ données React pour les timestamps ChatGPT)
              4. parsing -> Conversation standardisée + validation
              5. écriture atomique exports/<date>/<service>/<id>.json
        └── état incrémentiel (.state/state.json) : les conversations inchangées
            ne sont pas ré-écrites (sauf --force)
```

- **Sessions conservées** : profils Playwright persistants dans `profiles/<service>/`
  (cookies de connexion gardés entre les exécutions).
- **Scroll infini** : la sidebar et le fil de discussion sont scrollés jusqu'à
  stabilisation (chargement complet de l'historique).
- **Isolation des pannes** : une conversation ou un service en échec
  n'arrête pas le reste ; le résumé final liste les échecs.
- **Logging structuré** : console lisible + `logs/aicv.jsonl` en JSON lines.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # binaire navigateur
```

## Première connexion (obligatoire, une fois par service)

L'export est headless et réutilise les cookies du profil persistant.
Il faut donc se connecter une première fois dans un navigateur visible :

```bash
.venv/bin/python run.py --login chatgpt
.venv/bin/python run.py --login claude
.venv/bin/python run.py --login gemini
.venv/bin/python run.py --login perplexity
```

Un navigateur s'ouvre : connectez-vous normalement, le script détecte la fin
de la connexion (navigation hors page de login) et ferme tout seul.
Les cookies sont sauvegardés dans `profiles/<service>/`.

Important : `--login` ouvre **le même moteur que l'export** du service
(Botasaurus pour Claude). Les cookies anti-bot comme `cf_clearance` sont liés
à l'User-Agent — une connexion Playwright ne serait pas réutilisable par
Botasaurus, et inversement.

## Utilisation

```bash
# tout exporter (services enabled dans config.yaml)
.venv/bin/python run.py --all

# un seul service
.venv/bin/python run.py --service chatgpt

# plusieurs services
.venv/bin/python run.py -s chatgpt -s claude

# conversations de la journée uniquement, dans exports/<date>/
.venv/bin/python run.py --all --date 2026-09-09

# debug : navigateur visible, 5 conversations max, logs verbeux
.venv/bin/python run.py --service gemini --headful --limit 5 --verbose

# ré-exporter même si rien n'a changé
.venv/bin/python run.py --all --force
```

Codes de sortie : `0` OK, `1` au moins un échec, `2` erreur d'usage/config.

## Planification quotidienne (cron)

```cron
30 3 * * * cd /chemin/du/depot && .venv/bin/python run.py --all >> logs/cron.log 2>&1
```

## Format JSON standardisé

Un fichier par conversation : `exports/<AAAA-MM-JJ>/<service>/<id>.json`

```json
{
  "service": "chatgpt",
  "conversation_id": "abc123",
  "title": "Titre de la conversation",
  "started_at": "2025-01-15T10:30:00Z",
  "last_message_at": "2025-01-15T11:45:00Z",
  "model": "gpt-4",
  "exported_at": "2025-06-09T00:00:00Z",
  "messages": [
    {
      "role": "user",
      "content": "Texte du message",
      "timestamp": "2025-01-15T10:30:00Z",
      "metadata": {}
    },
    {
      "role": "assistant",
      "content": "Réponse de l'IA",
      "timestamp": "2025-01-15T10:30:05Z",
      "metadata": {
        "model": "gpt-4",
        "tokens": null
      }
    }
  ]
}
```

- `role` ∈ `user | assistant | system | tool`
- timestamps ISO-8601 UTC (`...Z`) ou `null` quand la plateforme ne les expose pas
- `metadata` : libre par service (ChatGPT : modèle par message ; Claude :
  `had_thinking` ; Perplexity : `sources`)
- le markdown est préservé en texte brut, blocs `<pre>` convertis en fences \`\`\`

## Moteurs navigateur

Deux moteurs partagent la même façade (`goto`, `html`, `evaluate`, scroll,
détection/contournement anti-bot…) — les services ne voient pas la différence :

| moteur | rôle |
|---|---|
| `playwright` | Chromium piloté par Playwright (par défaut) |
| `botasaurus` | Chrome patché + `bypass_cloudflare` (contourne Cloudflare/Turnstile) |

```yaml
engine: auto        # auto = Playwright, bascule Botasaurus si challenge détecté
services:
  claude:
    engine: botasaurus   # surcharge par service (Cloudflare bloque claude.ai)
```

En `auto`, un `BlockedError` relance automatiquement le service avec
Botasaurus (une fois) — que ce soit à la découverte ou sur une conversation
en cours. Le chemin du Chrome Botasaurus est auto-détecté (Chromium
Playwright, ou `$AICV_CHROME_PATH`) : `botasaurus.chrome_executable_path`.

### Xvfb (recommandé)

Cloudflare détecte (et bloque) le headless sur certains services
(Perplexity). L'option `botasaurus.enable_xvfb: true` lance alors un Chrome
**headful dans un affichage virtuel Xvfb** (paquet `xvfb`) : invisible en
SSH, indétectable comme headless. Si Xvfb manque, repli automatique sur le
headless pur.

## Découverte des listes (le point critique)

Les sidebars ne montrent qu'un extrait de l'historique ; chaque service a sa
propresolution :

| service | découverte | nb. validé |
|---|---|---|
| ChatGPT | sidebar scrollée (collecteur incrémentiel) | 168 |
| Claude | sidebar (les IDs de conversation sont listés en entier) | 21 |
| Gemini | sidebar repliée (ouverte automatiquement) + scroll de l'`infinite-scroller` | 290+ |
| Perplexity | **API GraphQL de la library** (persisted query, pagination par curseur) | **347** |

Le collecteur incrémentiel (`_collect_sidebar_refs`) gère les listes
**virtualisées** (seuls les items visibles sont dans le DOM, les items
scrollés disparaissent) : il saute au bas de chaque conteneur scrollable et
accumule les refs à chaque tour jusqu'à stabilisation.

Sur Perplexity, les lignes virtuelles n'ont même pas de `<a>` — d'où la
découverte par GraphQL (`LibraryRecentThreadsPaginationQuery`, endpoint
`/rest/perplexity_ask/graphql`). Si la structure change, fallback DOM
automatique (limité au récent).

## Anti rate-limit

ChatGPT ferme l'accès à l'historique en cas de rafale (« Too many requests »).
Parades :

- `pacing_ms` : pause entre conversations (chatgpt : 1500 ms)
- détection du dialog de limite → fermeture (« Got it ») + attentes
  croissantes (15/30/60 s) avant rechargement
- `scripts/export_missing.py` : n'exporte que les conversations absentes du
  disque (utile après un throttling — évite de re-scroller toute la liste)

## Validation

Le pipeline complet a été validé sur les 4 services avec sessions réelles :
découverte complète des listes (168 chatgpt / 21 claude / 290 gemini /
347 perplexity), export headless, contournement Cloudflare (Claude et
Perplexity via Botasaurus/Xvfb), parsing et écriture JSON — 848 messages
chatgpt, 76 claude, 134 gemini, 1 345 perplexity sur la session de test,
liens en markdown et blocs de code en fences vérifiés par :

```bash
.venv/bin/python scripts/audit_exports.py exports   # liens, fences, artefacts DOM restants
.venv/bin/python scripts/export_missing.py <service>  # export des manquantes seulement
```

## Configuration

Tout est dans [`config.yaml`](config.yaml) : services activés + URL, répertoires
(`output_dir`, `profile_dir`, `state_file`, `log_file`), headless, timeouts,
paramètres de scroll. Les valeurs omises reprennent `DEFAULT_CONFIG`
(`src/orchestrator.py`).

## Architecture

```
├── run.py                     # point d'entrée CLI
├── config.yaml                # configuration (services, chemins, schedule)
├── requirements.txt
├── scripts/
│   └── audit_exports.py       # audit qualité des JSON (liens, code, artefacts)
├── src/
│   ├── orchestrator.py        # workflow principal (pipeline + bascule moteurs)
│   ├── browser.py             # moteur Playwright (profil persistant, scroll)
│   ├── browser_botasaurus.py  # moteur Botasaurus (anti-Cloudflare, même façade)
│   ├── selectors.py           # traduction sélecteurs Playwright -> CSS (botasaurus)
│   ├── schema.py              # schéma JSON standardisé (dataclasses + validation)
│   ├── services/              # scraping par plateforme (sélecteurs, pipeline)
│   │   ├── base.py            #   BaseService : découverte + scrape + parse
│   │   ├── chatgpt.py         #   + extraction des timestamps via props React
│   │   ├── claude.py / gemini.py / perplexity.py
│   ├── parsers/               # parsing DOM (BeautifulSoup, testable sans navigateur)
│   │   ├── base.py            #   BaseParser : parse(), parse_links(), text_of()
│   │   └── <service>.py       #   sélecteurs CSS réels par plateforme
│   └── utils/
│       ├── logging.py         # logging structuré (console + JSON lines)
│       └── file_utils.py      # exports atomiques, dates, état incrémentiel
├── tests/                     # pytest : schéma, parsers (fixtures HTML),
│   └── fixtures/              # moteurs (traducteur sélecteurs), orchestrator, CLI
├── profiles/                  # profils navigateurs (ignorés git)
├── .state/state.json          # incrémental (ignoré git)
├── logs/                      # aicv.jsonl (ignoré git)
└── exports/                   # sorties (ignorées git)
```

## Tests

Les parsers sont testés **sans navigateur** sur des fixtures HTML reproduisant
le DOM réel de chaque plateforme ; l'orchestrateur est testé avec des services
et sessions factices injectés ; le CLI est testé sans lancer Chromium.

```bash
.venv/bin/python -m pytest
```

## Dépannage

| Symptôme | Cause / solution |
|---|---|
| `session expirée` / service `SKIP` | relancer `run.py --login <service>` |
| `bloque par un challenge anti-bot` | le cookie `cf_clearance` a expiré → refaire `--login <service>` ; vérifier que `enable_xvfb: true` |
| `aucun message reconnu` | DOM de la plateforme changé → mettre à jour les sélecteurs dans `src/parsers/<service>.py` (chaînes de fallback) |
| `Executable doesn't exist` | `.venv/bin/python -m playwright install chromium` |
| Export vide | vérifier `profiles/` (session), et lancer `--headful --verbose` pour observer |
| `Xvfb indisponible` dans les logs | `.venv/bin/pip install` non requis — installer le paquet système `xvfb` |

## Limites connues

- ChatGPT : timestamps absents du DOM → récupérés depuis les props internes
  React (best effort) ; sans eux, `started_at`/`last_message_at` sont `null`.
- Claude : le DOM a deux générations (classique + « transcript » 2026, gérées
  toutes deux) ; le badge modèle a disparu du nouveau DOM (`model: null`) ;
  les conversations qui ne chargent aucun message (tâches, vides) sont
  **ignorées** sans faire échouer le run (`ignorees=` dans le résumé).
- Gemini : la sidebar démarre parfois repliée (ouverte automatiquement) ;
  l'historique complet est derrière « Show all » / « Tout afficher » (cliqué
  automatiquement si présent) ; le lazy-loading du fil ne remonte pas
  toujours très loin.
- Perplexity : Cloudflare refuse le headless pur → le moteur Botasaurus est
  sollicité (`engine: botasaurus`), avec Xvfb. Le forfait **gratuit** rate-limite
  la lecture en rafale des conversations (redirection vers l'accueil) :
  espace les runs (cron quotidien = OK) ou reprend avec
  `scripts/export_missing.py` ; les timestamps sont `null` (non exposés) et
  le modèle vient de l'API de découverte (`displayModel.modelID`).
- Les DOM des plateformes changent souvent : les parsers ont des chaînes de
  fallback, mais une rupture DOM demande une mise à jour des sélecteurs.
- La fiabilité repose sur la **convergence** : listing sidebar et rendu des
  fils sont parfois floppyeux (listes vides/partielles, SPA lente) — chaque
  étape retente (reload, 2e listing, 2e rendu), et les échecs résiduels
  d'une exécution sont repris à la suivante (l'état incrémentiel complète).
