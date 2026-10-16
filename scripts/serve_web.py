#!/usr/bin/env python3
"""Serveur local pour la page de recherche dans les conversations.

Sert la page web (web/) et une petite API JSON :
  - GET /api/stats
  - GET /api/filters
  - GET /api/activity
  - GET /api/day?date=AAAA-MM-JJ
  - GET /api/search?q=&k=&alpha=&platforms=&model=&date_from=&date_to=
    (q vide + au moins un critere = recherche par criteres seuls, sans score)
  - GET /api/conversations?platform=
  - GET /api/conversation?platform=&id=
  - GET /api/random?n=

La recherche combine similarite cosinus (BGE-M3 + sqlite-vec) et mots-cles.

    .venv/bin/python scripts/serve_web.py
    .venv/bin/python scripts/serve_web.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.rag.embeddings import model_dim  # noqa: E402
from src.rag.search import combined_search  # noqa: E402
from src.rag.store import VectorStore  # noqa: E402
from src.utils.urls import conversation_url  # noqa: E402

WEB_DIR = ROOT / "web"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".img": "application/octet-stream",
}

MEDIA_RE = re.compile(r"^/media/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")

#: plateformes servies (liste blanche : jamais de nom arbitraire dans un chemin)
PLATFORMS = ("chatgpt", "claude", "gemini", "perplexity", "grok", "mistral")
#: identifiants de conversation acceptes (pas de separateur de chemin)
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
#: fenetre de la heatmap d'activite (jours, jour courant inclus)
ACTIVITY_DAYS = 365
#: bornes du tirage aleatoire (/api/random)
RANDOM_MAX = 8
#: longueur de l'extrait de message dans la vue « journee » (/api/day)
DAY_EXTRACT_LEN = 300
#: nombre maxi de messages renvoyes par une recherche par criteres seuls
FILTER_LIMIT = 200
#: date AAAA-MM-JJ (validation stricte, verifiee ensuite par strptime)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    """Parametre de requete invalide -> reponse JSON + statut HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def check_platform(platform: str) -> str:
    """Valide un nom de plateforme contre la liste blanche."""
    if platform not in PLATFORMS:
        raise ApiError(f"plateforme inconnue: {platform!r}")
    return platform


def check_conversation_id(conversation_id: str) -> str:
    """Valide un identifiant de conversation (pas de separateur de chemin)."""
    if not CONVERSATION_ID_RE.match(conversation_id or ""):
        raise ApiError("identifiant de conversation invalide")
    return conversation_id


def check_date(value: str) -> str:
    """Valide une date calendaire reelle au format AAAA-MM-JJ."""
    if not DATE_RE.match(value or ""):
        raise ApiError("date invalide (format AAAA-MM-JJ attendu)")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ApiError(f"date invalide: {value!r}") from exc
    return value


def message_extract(texte: str, limit: int = DAY_EXTRACT_LEN) -> str:
    """Extrait d'une ligne (~300 caracteres), coupe au mot, ellipse en fin."""
    flat = re.sub(r"\s+", " ", texte or "").strip()
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut + "…"


class AppState:
    """Etat partage : store + embedder charges paresseusement (thread-safe)."""

    def __init__(self, db_path: Path, exports_dir: Path, model_name: str, dim: int = 0):
        self.db_path = db_path
        self.exports_dir = exports_dir
        self.model_name = model_name
        self.dim = model_dim(model_name, dim)
        self._store = None
        self._embedder = None
        self._lock = threading.Lock()
        # serialise l'acces store/modele entre requetes concurrentes
        self.api_lock = threading.Lock()

    def store(self) -> VectorStore:
        with self._lock:
            if self._store is None:
                self._store = VectorStore(self.db_path, dim=self.dim)
            return self._store

    def embedder(self):
        with self._lock:
            if self._embedder is None:
                from src.rag.embeddings import Embedder

                print("[web] chargement du modele d'embeddings (1re recherche)...")
                self._embedder = Embedder(self.model_name, dim=self.dim)
            return self._embedder

    def embed(self, text: str):
        return self.embedder().encode_one(text)

    def conversation_file(self, platform: str, conversation_id: str):
        # defense en profondeur : jamais de chemin hors de exports/<plateforme>
        if platform not in PLATFORMS:
            return None
        base = (self.exports_dir / platform).resolve()
        root = self.exports_dir.resolve()
        if root not in base.parents:
            return None
        store = self.store()
        path = store.conversation_file(platform, conversation_id)
        if path and Path(path).exists():
            return Path(path)
        for candidate in base.glob("*.json"):
            if candidate.name == "conversation_list.json":
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(data.get("conversation_id")) == str(conversation_id):
                return candidate
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "AICV/1.0"
    state: AppState = None  # injecte au demarrage

    # -- helpers ---------------------------------------------------------------

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header(
            "Content-Type",
            CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[web] %s\n" % (fmt % args))

    # -- routes ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                self._handle_api(path, parse_qs(parsed.query))
            elif path.startswith("/media/"):
                self._serve_media(path)
            elif path in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html")
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _serve_media(self, path: str) -> None:
        """Sert une image telechargee : exports/<platform>/images/<fichier>."""
        match = MEDIA_RE.match(path)
        if not match:
            self._send_json({"error": "forbidden"}, 403)
            return
        platform, filename = match.groups()
        if platform not in PLATFORMS:
            self._send_json({"error": "forbidden"}, 403)
            return
        base = self.state.exports_dir / platform
        for subdir in ("images", "files"):
            root = (base / subdir).resolve()
            candidate = (root / filename).resolve()
            if root in candidate.parents and candidate.is_file():
                self._send_file(candidate)
                return
        self._send_json({"error": "not found"}, 404)

    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/")
        candidate = (WEB_DIR / relative).resolve()
        if WEB_DIR not in candidate.parents and candidate != WEB_DIR:
            self._send_json({"error": "forbidden"}, 403)
            return
        if not candidate.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        self._send_file(candidate)

    def _handle_api(self, path: str, params) -> None:
        with self.state.api_lock:
            if path == "/api/stats":
                self._send_json(self._stats())
            elif path == "/api/filters":
                self._send_json(self._filters())
            elif path == "/api/activity":
                self._send_json(self._activity())
            elif path == "/api/day":
                self._send_json(self._day(params))
            elif path == "/api/search":
                self._send_json(self._search(params))
            elif path == "/api/conversations":
                platform = (params.get("platform") or [""])[0]
                self._send_json(self._conversations(platform))
            elif path == "/api/conversation":
                platform = (params.get("platform") or [""])[0]
                cid = (params.get("id") or [""])[0]
                self._send_json(self._conversation(platform, cid))
            elif path == "/api/random":
                self._send_json(self._random(params))
            else:
                self._send_json({"error": "unknown endpoint"}, 404)

    # -- endpoints -------------------------------------------------------------

    def _stats(self):
        store = self.state.store()
        counts = {
            row["platform"]: {"messages": row["n"]}
            for row in store.db.execute(
                "SELECT platform, COUNT(*) AS n FROM messages GROUP BY platform"
            ).fetchall()
        }
        convs = {
            row["platform"]: row["n"]
            for row in store.db.execute(
                "SELECT platform, COUNT(*) AS n FROM conversations GROUP BY platform"
            ).fetchall()
        }
        for platform, data in counts.items():
            data["conversations"] = convs.get(platform, 0)
        model_counts = [
            {"model": row["model"], "count": row["n"]}
            for row in store.db.execute(
                """
                SELECT model, COUNT(*) AS n FROM messages
                WHERE model IS NOT NULL AND model != ''
                GROUP BY model ORDER BY n DESC, model LIMIT 8
                """
            ).fetchall()
        ]
        return {
            "platforms": counts,
            "total_messages": sum(d["messages"] for d in counts.values()),
            "models": store.distinct_models(),
            "model_counts": model_counts,
        }

    def _activity(self):
        """Messages par jour sur les 365 derniers jours (heatmap d'activite).

        Tous les jours de la fenetre sont presents (y compris a zero), ce qui
        evite au front de completer les trous.
        """
        store = self.state.store()
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=ACTIVITY_DAYS - 1)
        rows = store.db.execute(
            """
            SELECT date(timestamp) AS day, COUNT(*) AS n FROM messages
            WHERE timestamp != '' AND timestamp IS NOT NULL AND date(timestamp) >= ?
            GROUP BY day
            """,
            (start.isoformat(),),
        ).fetchall()
        counts = {row["day"]: row["n"] for row in rows}
        days = {}
        day = start
        while day <= end:
            key = day.isoformat()
            days[key] = counts.get(key, 0)
            day += timedelta(days=1)
        return {"days": days, "max": max(days.values(), default=0)}

    def _day(self, params):
        """Messages d'une journee, groupes par conversation (vue « journee »)."""
        date = check_date((params.get("date") or [""])[0])
        store = self.state.store()
        rows = store.db.execute(
            """
            SELECT message_id, conversation_id, platform, role, model, timestamp, texte
            FROM messages WHERE date(timestamp) = ?
            ORDER BY timestamp, rowid
            """,
            (date,),
        ).fetchall()
        groups: dict = {}
        order: list = []
        for row in rows:
            key = (row["platform"], row["conversation_id"])
            group = groups.get(key)
            if group is None:
                group = {
                    "platform": row["platform"],
                    "conversation_id": row["conversation_id"],
                    "title": "",
                    "url": conversation_url(row["platform"], row["conversation_id"]),
                    "message_count": 0,
                    "messages": [],
                }
                groups[key] = group
                order.append(key)
            group["message_count"] += 1
            group["messages"].append(
                {
                    # identifiant vide -> null (le front ouvre sans ancre)
                    "message_id": row["message_id"] or None,
                    "role": row["role"],
                    "model": row["model"] or None,
                    "timestamp": row["timestamp"],
                    "texte": message_extract(row["texte"]),
                }
            )
        # titres/URL depuis l'index (repli : titre vide, URL calculee)
        meta = store.conversation_meta(order)
        for key in order:
            info = meta.get(key) or {}
            group = groups[key]
            group["title"] = info.get("title") or ""
            if info.get("url"):
                group["url"] = info["url"]
        conversations = [groups[key] for key in order]
        return {
            "date": date,
            "message_count": len(rows),
            "conversation_count": len(conversations),
            "conversations": conversations,
        }

    def _filters(self):
        store = self.state.store()
        platforms = sorted(
            d.name
            for d in self.state.exports_dir.iterdir()
            if d.is_dir() and (d / "conversation_list.json").exists()
        )
        lo, hi = store.date_bounds()
        return {
            "platforms": platforms,
            "models": store.distinct_models(),
            "date_min": lo,
            "date_max": hi,
        }

    def _search(self, params):
        query = (params.get("q") or [""])[0].strip()
        try:
            k = int((params.get("k") or ["30"])[0])
        except ValueError as exc:
            raise ApiError("parametre k invalide (entier attendu)") from exc
        try:
            alpha = float((params.get("alpha") or ["0.6"])[0])
        except ValueError as exc:
            raise ApiError("parametre alpha invalide (nombre attendu)") from exc
        if not 1 <= k <= 500:
            raise ApiError("k doit etre entre 1 et 500")
        if not 0.0 <= alpha <= 1.0:
            raise ApiError("alpha doit etre entre 0 et 1")
        platforms_raw = (params.get("platforms") or [""])[0]
        platforms = [p for p in platforms_raw.split(",") if p] or None
        for platform in platforms or ():
            check_platform(platform)
        model = (params.get("model") or [""])[0] or None
        date_from = (params.get("date_from") or [""])[0] or None
        date_to = (params.get("date_to") or [""])[0] or None
        if not query:
            # sans texte : criteres seuls ; sans critere non plus, reponse vide
            if not (platforms or model or date_from or date_to):
                return {"query": query, "count": 0, "group_count": 0, "groups": []}
            return self._search_filter_only(platforms, model, date_from, date_to)
        vector = self.state.embed(query)
        results = combined_search(
            self.state.store(),
            vector,
            query,
            k=k,
            alpha=alpha,
            platforms=platforms,
            model=model,
            date_from=date_from,
            date_to=date_to,
        )
        return self._group_results(query, results)

    def _search_filter_only(self, platforms, model, date_from, date_to):
        """Recherche par criteres seuls (q vide) : tri par date, sans score.

        Meme forme de reponse que la recherche combinee (groupes par
        conversation) mais `score`/`semantic_score`/`keyword_score` sont null
        et `best_score` est null. Plage de dates inclusive (`date(timestamp)`),
        limite FILTER_LIMIT messages.
        """
        store = self.state.store()
        clauses: list = []
        params: list = []
        if platforms:
            clauses.append("platform IN (" + ",".join("?" * len(platforms)) + ")")
            params.extend(platforms)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if date_from:
            clauses.append("date(timestamp) >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("date(timestamp) <= ?")
            params.append(date_to)
        where = " AND ".join(clauses) if clauses else "1"
        rows = store.db.execute(
            f"""
            SELECT message_id, conversation_id, platform, role, model, timestamp, texte
            FROM messages WHERE {where}
            ORDER BY timestamp DESC, rowid DESC LIMIT ?
            """,
            params + [FILTER_LIMIT],
        ).fetchall()
        groups: dict = {}
        for row in rows:
            key = (row["platform"], row["conversation_id"])
            group = groups.get(key)
            if group is None:
                group = {
                    "platform": row["platform"],
                    "conversation_id": row["conversation_id"],
                    "url": conversation_url(row["platform"], row["conversation_id"]),
                    "title": "",
                    "messages": [],
                    "best_score": None,
                }
                groups[key] = group
            group["messages"].append(
                {
                    "message_id": row["message_id"] or "",
                    "role": row["role"],
                    "model": row["model"] or None,
                    "timestamp": row["timestamp"],
                    "texte": row["texte"] or "",
                    "score": None,
                    "semantic_score": None,
                    "keyword_score": None,
                }
            )
        meta = store.conversation_meta(list(groups.keys()))
        ordered = []
        for key, group in groups.items():
            info = meta.get(key) or {}
            group["title"] = info.get("title") or ""
            if info.get("url"):
                group["url"] = info["url"]
            group["hit_count"] = len(group["messages"])
            ordered.append(group)
        # conversations les plus recentes d'abord (messages deja tries desc)
        ordered.sort(key=lambda g: g["messages"][0]["timestamp"] or "", reverse=True)
        return {
            "query": "",
            "filtered": True,
            "count": len(rows),
            "group_count": len(ordered),
            "groups": ordered,
        }

    def _group_results(self, query: str, results):
        """Regroupe les messages par conversation (multi-hits d'abord)."""
        groups: dict = {}
        for item in results:
            key = (item["platform"], item["conversation_id"])
            group = groups.get(key)
            if group is None:
                group = {
                    "platform": item["platform"],
                    "conversation_id": item["conversation_id"],
                    "url": conversation_url(item["platform"], item["conversation_id"]),
                    "title": "",
                    "messages": [],
                    "best_score": 0.0,
                }
                groups[key] = group
            group["messages"].append(item)
            group["best_score"] = max(group["best_score"], item["score"])
        meta = self.state.store().conversation_meta(list(groups.keys()))
        for key, group in groups.items():
            info = meta.get(key) or {}
            group["title"] = info.get("title") or ""
            if info.get("url"):
                group["url"] = info["url"]
            group["messages"].sort(key=lambda m: m["score"], reverse=True)
            group["hit_count"] = len(group["messages"])
            group["best_score"] = round(group["best_score"], 4)
        # conversations avec plusieurs messages pertinents d'abord, puis isoles
        ordered = sorted(
            groups.values(),
            key=lambda g: (0 if g["hit_count"] >= 2 else 1, -g["best_score"]),
        )
        return {
            "query": query,
            "count": len(results),
            "group_count": len(ordered),
            "groups": ordered,
        }

    def _random(self, params):
        """Une (n=1, format historique) ou plusieurs conversations au hasard."""
        raw = (params.get("n") or [""])[0]
        n = 1
        if raw:
            try:
                n = int(raw)
            except ValueError as exc:
                raise ApiError("parametre n invalide (entier attendu)") from exc
        if not 1 <= n <= RANDOM_MAX:
            raise ApiError(f"n doit etre entre 1 et {RANDOM_MAX}")
        store = self.state.store()
        picked: list = []
        seen = set()
        # tirage avec deduplication : bornage des tentatives (pool petit possible)
        for _ in range(n * 12 + 12):
            if len(picked) >= n:
                break
            row = store.random_conversation()
            if not row:
                break
            key = (row.get("platform"), row.get("conversation_id"))
            if key in seen:
                continue
            seen.add(key)
            if not row.get("url"):
                row["url"] = conversation_url(row["platform"], row["conversation_id"])
            picked.append(row)
        self._enrich_random(picked)
        if n == 1:
            # format historique (bouton « Aleatoire ») : objet simple
            return picked[0] if picked else {"error": "aucune conversation"}
        return {"conversations": picked}

    def _enrich_random(self, rows) -> None:
        """Ajoute message_count + date du dernier message aux conversations tirees."""
        if not rows:
            return
        store = self.state.store()
        pairs = [(row["platform"], row["conversation_id"]) for row in rows]
        values = ",".join("(?,?)" for _ in pairs)
        flat = [value for pair in pairs for value in pair]
        counts = {
            (row["platform"], row["conversation_id"]): row["message_count"]
            for row in store.db.execute(
                f"""
                SELECT platform, conversation_id, message_count FROM conversations
                WHERE (platform, conversation_id) IN (VALUES {values})
                """,
                flat,
            ).fetchall()
        }
        lasts = {
            (row["platform"], row["conversation_id"]): row["last_ts"]
            for row in store.db.execute(
                f"""
                SELECT platform, conversation_id, MAX(timestamp) AS last_ts
                FROM messages
                WHERE (platform, conversation_id) IN (VALUES {values})
                GROUP BY platform, conversation_id
                """,
                flat,
            ).fetchall()
        }
        for row in rows:
            key = (row["platform"], row["conversation_id"])
            row["message_count"] = counts.get(key)
            row["last_message_at"] = lasts.get(key) or None

    def _conversations(self, platform: str):
        if platform:
            check_platform(platform)
        platforms = [platform] if platform else sorted(
            d.name
            for d in self.state.exports_dir.iterdir()
            if d.is_dir() and (d / "conversation_list.json").exists()
        )
        items = []
        for name in platforms:
            path = self.state.exports_dir / name / "conversation_list.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for entry in data.get("conversations") or []:
                conversation_id = entry.get("conversation_id")
                items.append(
                    {
                        "platform": name,
                        "conversation_id": conversation_id,
                        "title": entry.get("title") or "",
                        "url": entry.get("url")
                        or conversation_url(name, conversation_id),
                        "message_count": entry.get("message_count"),
                        "last_message_at": entry.get("last_message_at"),
                        "has_code": entry.get("has_code"),
                        "scraped": entry.get("scraped"),
                    }
                )
        items.sort(key=lambda x: (x.get("last_message_at") or ""), reverse=True)
        return {"count": len(items), "conversations": items}

    def _conversation(self, platform: str, conversation_id: str):
        if not platform or not conversation_id:
            raise ApiError("platform et id requis")
        check_platform(platform)
        check_conversation_id(conversation_id)
        path = self.state.conversation_file(platform, conversation_id)
        if path is None:
            return {"error": "conversation introuvable"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"error": str(exc)}
        return {
            "platform": platform,
            "conversation_id": conversation_id,
            "file": str(path),
            "url": data.get("url") or conversation_url(platform, conversation_id),
            "conversation": data,
        }


def _open_browser(url: str) -> None:
    """Ouvre l'URL dans Chrome si dispo, sinon le navigateur par defaut."""
    import shutil
    import subprocess
    import webbrowser

    for name in (
        "google-chrome", "google-chrome-stable", "chromium",
        "chromium-browser", "brave-browser", "microsoft-edge",
    ):
        path = shutil.which(name)
        if path:
            try:
                subprocess.Popen(
                    [path, url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:  # noqa: BLE001
                pass
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true",
                        help="ne pas ouvrir le navigateur au demarrage")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--exports", type=Path)
    parser.add_argument("--model")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")

    def resolve(value) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    Handler.state = AppState(
        resolve(args.db or config["rag_db"]),
        resolve(args.exports or config["output_dir"]),
        args.model or config.get("rag_model"),
        config.get("rag_dim") or 0,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}"
    print(f"[web] {url}  (Ctrl+C pour arreter)")
    if not args.no_open:
        threading.Timer(1.0, _open_browser, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] arret")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
