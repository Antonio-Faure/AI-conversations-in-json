#!/usr/bin/env python3
"""Serveur local pour la page de recherche dans les conversations.

Sert la page web (web/) et une petite API JSON :
  - GET /api/stats
  - GET /api/filters
  - GET /api/search?q=&k=&alpha=&platforms=&model=&date_from=&date_to=
  - GET /api/conversations?platform=
  - GET /api/conversation?platform=&id=

La recherche combine similarite cosinus (BGE-M3 + sqlite-vec) et mots-cles.

    .venv/bin/python scripts/serve_web.py
    .venv/bin/python scripts/serve_web.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
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
}


class AppState:
    """Etat partage : store + embedder charges paresseusement (thread-safe)."""

    def __init__(self, db_path: Path, exports_dir: Path, model_name: str):
        self.db_path = db_path
        self.exports_dir = exports_dir
        self.model_name = model_name
        self._store = None
        self._embedder = None
        self._lock = threading.Lock()
        # serialise l'acces store/modele entre requetes concurrentes
        self.api_lock = threading.Lock()

    def store(self) -> VectorStore:
        with self._lock:
            if self._store is None:
                self._store = VectorStore(self.db_path)
            return self._store

    def embedder(self):
        with self._lock:
            if self._embedder is None:
                from src.rag.embeddings import Embedder

                print("[web] chargement du modele d'embeddings (1re recherche)...")
                self._embedder = Embedder(self.model_name)
            return self._embedder

    def embed(self, text: str):
        return self.embedder().encode_one(text)

    def conversation_file(self, platform: str, conversation_id: str):
        store = self.store()
        path = store.conversation_file(platform, conversation_id)
        if path and Path(path).exists():
            return Path(path)
        for candidate in (self.exports_dir / platform).glob("*.json"):
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
            elif path in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html")
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

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
                self._send_json(self._random())
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
        return {
            "platforms": counts,
            "total_messages": sum(d["messages"] for d in counts.values()),
            "models": store.distinct_models(),
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
        if not query:
            return {"query": query, "count": 0, "group_count": 0, "groups": []}
        k = int((params.get("k") or ["30"])[0])
        alpha = float((params.get("alpha") or ["0.6"])[0])
        platforms_raw = (params.get("platforms") or [""])[0]
        platforms = [p for p in platforms_raw.split(",") if p] or None
        model = (params.get("model") or [""])[0] or None
        date_from = (params.get("date_from") or [""])[0] or None
        date_to = (params.get("date_to") or [""])[0] or None
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

    def _random(self):
        row = self.state.store().random_conversation()
        if not row:
            return {"error": "aucune conversation"}
        if not row.get("url"):
            row["url"] = conversation_url(row["platform"], row["conversation_id"])
        return row

    def _conversations(self, platform: str):
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
            return {"error": "platform et id requis"}
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
