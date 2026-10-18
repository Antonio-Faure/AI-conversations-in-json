#!/usr/bin/env python3
"""Serveur local pour la page de recherche dans les conversations.

Sert la page web (web/) et une petite API JSON :
  - GET /api/stats
  - GET /api/filters
  - GET /api/activity
  - GET /api/timeline
    (conversations par mois — 24 derniers mois — par plateforme, par modele et
     toutes plateformes ; `trend` = dernier mois complet vs le precedent)
  - GET /api/similar?platform=&id=&mid=&i=&k=
    (messages les plus proches d'un message, meme espace vectoriel)
  - GET /api/media?platform=&kind=&offset=&limit=
    (galerie des medias indexes : pagination et filtres)
  - GET /api/status
    (index RAG, fraicheur du scrape par plateforme, sante des API, sauvegardes)
  - GET /api/recheck
    (etat du controle de parseur en cours + derniers rapports `recheck.json`)
  - POST /api/recheck {"platforms": [...], "per_platform": 3, "min_messages": 8}
    (lance un controle de parseur en tache de fond — page « Test des parseurs »)
  - GET /api/scrape
    (etat du scrape a la demande : en cours, journal, bilan par plateforme)
  - POST /api/scrape {"platforms": [...], "mode": "daily", "limit": null}
    (lance un scrape en tache de fond — page « Scrape quotidien »)
  - GET /api/day?date=AAAA-MM-JJ
  - GET /api/search?q=&k=&alpha=&platforms=&model=&date_from=&date_to=&media_k=
    (q vide + au moins un critere = recherche par criteres seuls, sans score ;
     `media` = medias proches de la requete, meme espace vectoriel que le texte)
  - GET /api/conversations?platform=
  - GET /api/conversation?platform=&id=
  - GET /api/random?n=

La recherche combine similarite cosinus (EmbeddingGemma + sqlite-vec) et mots-cles.

    .venv/bin/python scripts/serve_web.py
    .venv/bin/python scripts/serve_web.py --port 9000 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from src.rag.media import MediaStore  # noqa: E402
from src.rag.search import combined_search  # noqa: E402
from src.rag.store import VectorStore  # noqa: E402
from src.utils.backup import BACKUP_DIR  # noqa: E402
from src.utils.recheck import (  # noqa: E402
    MIN_MESSAGES,
    PER_PLATFORM,
    RecheckJob,
    read_report,
)
from src.utils.scrape_job import DEFAULT_MODE, ScrapeJob  # noqa: E402
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
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".pdf": "application/pdf",
    ".csv": "text/csv; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}

#: signatures des images dont l'extension ne dit rien (`.img` telecharge tel quel)
IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_image_type(data: bytes) -> str | None:
    """Type MIME d'une image d'apres ses premiers octets (sinon None)."""
    for signature, mime in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None

MEDIA_RE = re.compile(r"^/media/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")

#: plateformes servies (liste blanche : jamais de nom arbitraire dans un chemin)
PLATFORMS = ("chatgpt", "claude", "gemini", "perplexity", "grok", "mistral")
#: identifiants de conversation acceptes (pas de separateur de chemin)
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
#: fenetre de la heatmap d'activite (jours, jour courant inclus)
ACTIVITY_DAYS = 365
#: fenetre des series temporelles (/api/timeline : mois, mois courant inclus)
TIMELINE_MONTHS = 24
#: nombre de modeles des series temporelles (/api/timeline)
TIMELINE_MODELS = 8
#: voisins renvoyes par /api/similar (defaut, borne haute)
SIMILAR_K_DEFAULT = 12
SIMILAR_K_MAX = 50
#: pagination de la galerie de medias (/api/media)
MEDIA_PAGE = 60
MEDIA_PAGE_MAX = 200
#: bornes du tirage aleatoire (/api/random)
RANDOM_MAX = 8
#: longueur de l'extrait de message dans la vue « journee » (/api/day)
DAY_EXTRACT_LEN = 300
#: nombre maxi de messages renvoyes par une recherche par criteres seuls
FILTER_LIMIT = 200
#: medias renvoyes par /api/search (defaut, borne haute)
MEDIA_K_DEFAULT = 8
MEDIA_K_MAX = 24
#: taille lue (tete et queue) pour la cle de contenu des medias (dedoublonnage)
MEDIA_HASH_BYTES = 256 * 1024


def media_content_key(path: Path) -> tuple[int, str] | None:
    """Cle de contenu d'un media (taille + md5 tete/queue) pour dedoublonner.

    Le meme media re-uploade dans plusieurs conversations produit des fichiers
    identiques (chemins differents) : la cle les regroupe. Un fichier illisible
    rend None (l'item est conserve tel quel).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    digest = hashlib.md5()
    try:
        with path.open("rb") as handle:
            digest.update(handle.read(MEDIA_HASH_BYTES))
            if size > 2 * MEDIA_HASH_BYTES:
                handle.seek(-MEDIA_HASH_BYTES, os.SEEK_END)
                digest.update(handle.read(MEDIA_HASH_BYTES))
    except OSError:
        return None
    return (size, digest.hexdigest())
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


def month_window(today, count: int) -> list:
    """`count` mois « AAAA-MM » consecutifs, mois de `today` inclus."""
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    months.reverse()
    return months


def month_trend(series: list, months: list):
    """Variation du dernier mois complet vs le precedent (None si incalculable).

    Le mois courant est incomplet : il est ignore, sinon la tendance serait
    faussement negative en debut de mois.
    """
    if len(series) < 3 or len(months) < 3:
        return None
    previous, last = series[-3], series[-2]
    if previous <= 0:
        return None
    return {
        "from": months[-3],
        "to": months[-2],
        "pct": round((last - previous) / previous * 100, 1),
    }


class AppState:
    """Etat partage : store + embedder charges paresseusement (thread-safe)."""

    def __init__(self, db_path: Path, exports_dir: Path, model_name: str, dim: int = 0,
                 config: dict | None = None):
        self.db_path = db_path
        self.exports_dir = exports_dir
        self.model_name = model_name
        #: config complete (le controle de parseur relance un scrape)
        self.config = config or {}
        self.dim = model_dim(model_name, dim)
        self._store = None
        self._media_store = None
        self._embedder = None
        #: cles de contenu des medias (dedoublonnage /api/search), par fichier
        self._media_keys: dict = {}
        self._lock = threading.Lock()
        # serialise l'acces store/modele entre requetes concurrentes
        self.api_lock = threading.Lock()
        #: controle de parseur lance depuis la page de test (un seul a la fois)
        self.recheck_job = RecheckJob()
        #: scrape a la demande lance depuis la page (un seul a la fois)
        self.scrape_job = ScrapeJob()

    def store(self) -> VectorStore:
        with self._lock:
            if self._store is None:
                self._store = VectorStore(self.db_path, dim=self.dim)
            return self._store

    def media_store(self) -> MediaStore:
        """Index des medias (memes base et dimension que les messages)."""
        with self._lock:
            if self._media_store is None:
                self._media_store = MediaStore(self.db_path, self.dim)
            return self._media_store

    def media_key(self, rel_path: str) -> tuple[int, str] | None:
        """Cle de contenu d'un media indexe (cache par chemin+taille+mtime)."""
        path = self.exports_dir / rel_path
        try:
            stat = path.stat()
        except OSError:
            return None
        cache_key = (rel_path, stat.st_size, stat.st_mtime_ns)
        if cache_key not in self._media_keys:
            self._media_keys[cache_key] = media_content_key(path)
        return self._media_keys[cache_key]

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
        content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        if content_type == "application/octet-stream":
            # telechargements sans extension fiable (.img) : type devine
            content_type = sniff_image_type(body) or content_type
        self.send_response(200)
        self.send_header("Content-Type", content_type)
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

    def do_POST(self) -> None:  # noqa: N802
        """Endpoints d'action (lancement du controle de parseur)."""
        path = urlparse(self.path).path
        try:
            if not path.startswith("/api/"):
                self._send_json({"error": "not found"}, 404)
                return
            body = self._read_json_body()
            if path == "/api/recheck":
                self._send_json(self._start_recheck(body))
            elif path == "/api/scrape":
                self._send_json(self._start_scrape(body))
            else:
                self._send_json({"error": "unknown endpoint"}, 404)
        except BrokenPipeError:
            pass
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def _read_json_body(self) -> dict:
        """Corps JSON de la requete (objet vide si absent)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 64 * 1024))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("corps JSON invalide") from exc
        if not isinstance(payload, dict):
            raise ApiError("objet JSON attendu")
        return payload

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
            elif path == "/api/timeline":
                self._send_json(self._timeline())
            elif path == "/api/similar":
                self._send_json(self._similar(params))
            elif path == "/api/media":
                self._send_json(self._media(params))
            elif path == "/api/status":
                self._send_json(self._status())
            elif path == "/api/recheck":
                self._send_json(self._recheck_status())
            elif path == "/api/scrape":
                self._send_json(self._scrape_status())
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

    def _timeline(self):
        """Conversations par mois (par plateforme et par modele).

        Une conversation est datee par le mois de son premier message
        horodate ; pour un modele, par le mois de sa premiere utilisation dans
        la conversation. Fenetre : les `TIMELINE_MONTHS` derniers mois (mois
        courant inclus) ; chaque serie est alignee sur `months` (zeros compris).
        """
        store = self.state.store()
        months = month_window(datetime.now(timezone.utc).date(), TIMELINE_MONTHS)
        first = months[0]
        platform_rows = store.db.execute(
            """
            SELECT platform, substr(start, 1, 7) AS mois, COUNT(*) AS n FROM (
                SELECT platform, conversation_id, MIN(timestamp) AS start
                FROM messages
                WHERE timestamp != '' AND timestamp IS NOT NULL
                GROUP BY platform, conversation_id
            ) WHERE mois >= ? GROUP BY platform, mois
            """,
            (first,),
        ).fetchall()
        model_rows = store.db.execute(
            """
            SELECT model, substr(mois, 1, 7) AS m, COUNT(*) AS n FROM (
                SELECT model, platform, conversation_id, MIN(timestamp) AS mois
                FROM messages
                WHERE model IS NOT NULL AND model != ''
                  AND timestamp != '' AND timestamp IS NOT NULL
                GROUP BY model, platform, conversation_id
            ) WHERE m >= ? GROUP BY model, m
            """,
            (first,),
        ).fetchall()

        platforms: dict = {}
        for row in platform_rows:
            platforms.setdefault(row["platform"], {})[row["mois"]] = row["n"]
        models: dict = {}
        for row in model_rows:
            models.setdefault(row["model"], {})[row["m"]] = row["n"]

        def series(counts: dict) -> list:
            return [counts.get(month, 0) for month in months]

        def entry(name_key: str, name: str, counts: dict) -> dict:
            values = series(counts)
            return {
                name_key: name,
                "series": values,
                "total": sum(values),
                "trend": month_trend(values, months),
            }

        platform_series = [entry("platform", name, counts)
                           for name, counts in platforms.items()]
        platform_series.sort(key=lambda item: (-item["total"], item["platform"]))
        model_series = [entry("model", name, counts)
                        for name, counts in models.items()]
        model_series.sort(key=lambda item: (-item["total"], item["model"]))
        all_counts: dict = {}
        for counts in platforms.values():
            for month, n in counts.items():
                all_counts[month] = all_counts.get(month, 0) + n
        totals = series(all_counts)
        return {
            "months": months,
            "platforms": platform_series,
            "models": model_series[:TIMELINE_MODELS],
            "total": {
                "series": totals,
                "total": sum(totals),
                "trend": month_trend(totals, months),
            },
        }

    def _similar(self, params):
        """Messages les plus proches d'un message donne (meme espace vectoriel).

        Le message est designe par `platform` + `id` (conversation) et `mid`
        (message_id) ou, a defaut, `i` (position dans la conversation). `k`
        borne le nombre de voisins ; le message d'origine est exclu.
        """
        platform = check_platform((params.get("platform") or [""])[0])
        conversation_id = check_conversation_id((params.get("id") or [""])[0])
        mid = (params.get("mid") or [""])[0]
        try:
            k = int((params.get("k") or [str(SIMILAR_K_DEFAULT)])[0])
        except ValueError as exc:
            raise ApiError("parametre k invalide (entier attendu)") from exc
        if not 1 <= k <= SIMILAR_K_MAX:
            raise ApiError(f"k doit etre entre 1 et {SIMILAR_K_MAX}")
        store = self.state.store()
        if mid:
            row = store.db.execute(
                """
                SELECT rowid FROM messages
                WHERE platform = ? AND conversation_id = ? AND message_id = ?
                ORDER BY rowid LIMIT 1
                """,
                (platform, conversation_id, mid),
            ).fetchone()
        else:
            try:
                offset = int((params.get("i") or ["0"])[0])
            except ValueError as exc:
                raise ApiError("parametre i invalide (entier attendu)") from exc
            row = store.db.execute(
                """
                SELECT rowid FROM messages
                WHERE platform = ? AND conversation_id = ?
                ORDER BY rowid LIMIT 1 OFFSET ?
                """,
                (platform, conversation_id, max(0, offset)),
            ).fetchone()
        if row is None:
            raise ApiError("message introuvable dans l'index")
        rowid = row["rowid"]
        vector = store.vectors_by_rowid([rowid]).get(rowid)
        if vector is None:
            raise ApiError("message sans vecteur dans l'index")
        # k + 1 puis exclusion du message d'origine : sqlite-vec applique `k`
        # avant le filtre `rowid`
        rows = store.db.execute(
            """
            SELECT m.message_id, m.conversation_id, m.platform, m.role, m.model,
                   m.timestamp, m.texte, v.distance AS distance
            FROM vec_messages v
            JOIN messages m ON m.rowid = v.rowid
            WHERE v.embedding MATCH ? AND k = ? AND v.rowid != ?
            ORDER BY v.distance LIMIT ?
            """,
            (vector, k + 1, rowid, k + 1),
        ).fetchall()
        results = []
        for item in rows:
            entry = dict(item)
            entry["score"] = 1.0 - float(entry.pop("distance"))
            results.append(entry)
        payload = self._group_results("", results[:k])
        payload["similar_to"] = {
            "platform": platform,
            "conversation_id": conversation_id,
            "message_id": mid or None,
        }
        return payload

    def _media(self, params):
        """Galerie des medias indexes (pagination + filtres)."""
        platform = (params.get("platform") or [""])[0]
        if platform:
            check_platform(platform)
        kind = (params.get("kind") or [""])[0]
        if kind and kind not in ("image", "audio", "video"):
            raise ApiError("kind doit etre image, audio ou video")
        try:
            offset = int((params.get("offset") or ["0"])[0])
            limit = int((params.get("limit") or [str(MEDIA_PAGE)])[0])
        except ValueError as exc:
            raise ApiError("offset/limit invalides (entiers attendus)") from exc
        if offset < 0:
            raise ApiError("offset doit etre positif")
        if not 1 <= limit <= MEDIA_PAGE_MAX:
            raise ApiError(f"limit doit etre entre 1 et {MEDIA_PAGE_MAX}")
        store = self.state.media_store()
        clauses, args = [], []
        if platform:
            clauses.append("platform = ?")
            args.append(platform)
        if kind:
            clauses.append("kind = ?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = store.db.execute(
            f"SELECT COUNT(*) AS n FROM media{where}", args
        ).fetchone()["n"]
        rows = store.db.execute(
            f"""
            SELECT media_id, platform, conversation_id, message_id, kind, path,
                   texte, indexed_at
            FROM media{where}
            ORDER BY indexed_at DESC, rowid DESC
            LIMIT ? OFFSET ?
            """,
            args + [limit, offset],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            filename = Path(item["path"]).name
            item["filename"] = filename
            item["url"] = f"/media/{item['platform']}/{filename}"
            items.append(item)
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    def _status(self):
        """Etat du chantier : index RAG, scrape par plateforme, sante, backups."""
        store = self.state.store()
        index = dict(store.stats())
        index["media"] = self.state.media_store().stats()
        row = store.db.execute(
            "SELECT MAX(indexed_at) AS last FROM conversations"
        ).fetchone()
        index["indexed_at"] = (row["last"] if row else None) or None
        db_path = Path(self.state.db_path)
        index["db_bytes"] = db_path.stat().st_size if db_path.is_file() else 0
        scrape = []
        for name in sorted(
            d.name for d in self.state.exports_dir.iterdir()
            if d.is_dir() and (d / "conversation_list.json").exists()
        ):
            path = self.state.exports_dir / name / "conversation_list.json"
            entry = {
                "platform": name,
                "conversations": 0,
                "last_message_at": None,
                "scraped_at": None,
                "health": self._api_health(name),
                "recheck": self._recheck(name),
            }
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entries = data.get("conversations") or []
                entry["conversations"] = len(entries)
                stamps = [
                    item.get("last_message_at") for item in entries
                    if item.get("last_message_at")
                ]
                entry["last_message_at"] = max(stamps) if stamps else None
                entry["scraped_at"] = datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (OSError, json.JSONDecodeError):
                pass
            scrape.append(entry)
        # verdict de controle de parseur le plus grave, pour le marquage du
        # tableau de bord (une plateforme critique suffit a alerter)
        order = {"critique": 3, "attention": 2, "ok": 1, "inconnu": 0}
        reports = [e["recheck"] for e in scrape if e.get("recheck")]
        return {
            "index": index,
            "scrape": scrape,
            "backups": self._backups(db_path),
            "recheck_verdict": (
                max((r["verdict"] for r in reports), key=lambda v: order.get(v, 0))
                if reports else None
            ),
            "recheck_at": max((r["checked_at"] or "" for r in reports), default=None) or None,
        }

    def _recheck(self, platform: str):
        """Dernier controle de parseur d'une plateforme (`recheck.json`)."""
        return read_report(self.state.exports_dir, platform)

    def _api_health(self, platform: str):
        """Sante de la forme des payloads API (`api_health.json`)."""
        path = self.state.exports_dir / platform / "api_health.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        n = data.get("n") or 0
        matches = data.get("matches") or 0
        return {
            "n": n,
            "matches": matches,
            "rate": round(matches / n * 100, 1) if n else None,
            "breaks": data.get("breaks") or 0,
            "drifts": data.get("drifts") or 0,
            "mean_score": data.get("mean_score"),
            "threshold": data.get("threshold"),
            "updated_at": data.get("updated_at"),
        }

    def _recheck_status(self):
        """Etat du controle de parseur en cours + derniers rapports connus."""
        state = self.state.recheck_job.snapshot()
        reports = []
        for platform in PLATFORMS:
            report = self._recheck(platform)
            if report:
                reports.append(report)
        return {**state, "reports": reports}

    def _start_recheck(self, body):
        """Lance un controle de parseur en tache de fond (page de test)."""
        raw = body.get("platforms")
        if raw is None:
            platforms = list(PLATFORMS)
        elif isinstance(raw, list):
            platforms = [check_platform(str(name)) for name in raw]
        else:
            raise ApiError("platforms doit etre une liste")
        if not platforms:
            raise ApiError("choisir au moins une plateforme")
        try:
            per_platform = int(body.get("per_platform", PER_PLATFORM))
            min_messages = int(body.get("min_messages", MIN_MESSAGES))
            seed = int(body.get("seed", 0))
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "per_platform/min_messages/seed doivent etre des entiers"
            ) from exc
        if not 1 <= per_platform <= 20:
            raise ApiError("per_platform doit etre entre 1 et 20")
        if not 1 <= min_messages <= 200:
            raise ApiError("min_messages doit etre entre 1 et 200")
        try:
            state = self.state.recheck_job.start(
                self.state.config, platforms, per_platform, min_messages, seed,
            )
        except RuntimeError as exc:
            raise ApiError(str(exc), 409) from exc
        return {"started": True, **state}

    def _scrape_status(self):
        """Etat du scrape a la demande (page « Scrape quotidien »)."""
        return self.state.scrape_job.snapshot()

    def _start_scrape(self, body):
        """Lance un scrape (daily/monthly) en tache de fond."""
        raw = body.get("platforms")
        if raw is None:
            platforms = list(PLATFORMS)
        elif isinstance(raw, list):
            platforms = [check_platform(str(name)) for name in raw]
        else:
            raise ApiError("platforms doit etre une liste")
        if not platforms:
            raise ApiError("choisir au moins une plateforme")
        mode = str(body.get("mode") or DEFAULT_MODE)
        if mode not in ("daily", "monthly"):
            raise ApiError("mode doit etre daily ou monthly")
        limit = body.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ApiError("limit doit etre un entier") from exc
            if not 1 <= limit <= 2000:
                raise ApiError("limit doit etre entre 1 et 2000")
        try:
            state = self.state.scrape_job.start(
                self.state.config, platforms, mode=mode, limit=limit,
            )
        except RuntimeError as exc:
            raise ApiError(str(exc), 409) from exc
        return {"started": True, **state}

    def _backups(self, db_path: Path):
        """Sauvegardes locales de la base (les plus recentes d'abord)."""
        backup_dir = db_path.parent / BACKUP_DIR
        if not backup_dir.is_dir():
            return []
        items = []
        for path in sorted(backup_dir.glob(f"{db_path.stem}-*.db"), reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({
                "name": path.name,
                "bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        return items

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
        try:
            media_k = int((params.get("media_k") or [str(MEDIA_K_DEFAULT)])[0])
        except ValueError as exc:
            raise ApiError("parametre media_k invalide (entier attendu)") from exc
        if not 1 <= media_k <= MEDIA_K_MAX:
            raise ApiError(f"media_k doit etre entre 1 et {MEDIA_K_MAX}")
        if not query:
            # sans texte : criteres seuls ; sans critere non plus, reponse vide
            if not (platforms or model or date_from or date_to):
                return {"query": query, "count": 0, "group_count": 0, "groups": [], "media": []}
            payload = self._search_filter_only(platforms, model, date_from, date_to)
            payload["media"] = []  # pas de vecteur requete : pas de medias
            return payload
        # un seul encodage : le meme vecteur sert au texte et aux medias
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
        payload = self._group_results(query, results)
        media = self._search_media(vector, platforms, media_k)
        # un media = un message a part entiere de sa conversation : il compte
        # dans la pertinence du groupe (score, extraits) ; seuls les medias
        # sans conversation restent des cartes autonomes
        payload["media"] = self._attach_media(payload["groups"], media)
        return payload

    def _attach_media(self, groups, media):
        """Rattache les medias a leur conversation (un media = un hit).

        Le media compte dans la pertinence du groupe (`best_score`, nombre
        d'extraits) : une conversation dont les messages *et* les medias sont
        pertinents remonte ; une conversation sans hit texte mais avec un
        media pertinent apparait. Rend les medias sans conversation
        (orphelins), tries par score.
        """
        by_key = {(g["platform"], g["conversation_id"]): g for g in groups}
        orphans = []
        for item in media:
            conversation_id = item.get("conversation_id") or None
            key = (item["platform"], conversation_id) if conversation_id else None
            group = by_key.get(key) if key else None
            if key and group is None:
                group = {
                    "platform": item["platform"],
                    "conversation_id": conversation_id,
                    "url": conversation_url(item["platform"], conversation_id),
                    "title": item.get("title") or "",
                    "messages": [],
                    "media": [],
                    "best_score": 0.0,
                }
                by_key[key] = group
                groups.append(group)
            if group is None:
                orphans.append(item)
                continue
            group.setdefault("media", [])
            group["media"].append(item)
            group["best_score"] = max(group["best_score"], item["score"] or 0.0)
        for group in groups:
            group.setdefault("media", [])
            group["media"].sort(key=lambda m: m["score"] or 0.0, reverse=True)
            group["media_count"] = len(group["media"])
            group["hit_count"] = len(group["messages"]) + group["media_count"]
            group["best_score"] = round(group["best_score"], 4)
        # plusieurs extraits d'abord, puis isolees (meme regle que _group_results)
        groups.sort(key=lambda g: (0 if g["hit_count"] >= 2 else 1, -g["best_score"]))
        orphans.sort(key=lambda m: m["score"] or 0.0, reverse=True)
        return orphans

    def _dedup_media(self, rows):
        """Retire les medias de contenu identique (le premier rencontre gagne)."""
        seen: set = set()
        unique = []
        for row in rows:
            key = self.state.media_key(row["path"] or "")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            unique.append(row)
        return unique

    def _search_media(self, vector, platforms, k: int):
        """Medias proches de la requete (meme espace vectoriel que le texte).

        `platforms` filtre sur media.platform (sur-echantillonnage puis
        troncature, MediaStore.search ne filtre que par type) ; `model` et les
        dates ne s'appliquent pas (les medias n'ont pas de timestamp).
        """
        # sur-echantillonnage : le dedoublonnage par contenu puis la troncature
        # a k doivent laisser assez de candidats distincts (memes medias
        # re-uploades dans plusieurs conversations).
        fetch = min(max(k * 6, 64), 192)
        try:
            rows = self.state.media_store().search(vector, k=fetch)
        except Exception as exc:  # noqa: BLE001
            # index medias absent/incompatible : la recherche texte reste servie
            print(f"[web] recherche medias indisponible: {exc}")
            return []
        rows = [row for row in rows if row["platform"] in PLATFORMS]
        if platforms:
            rows = [row for row in rows if row["platform"] in platforms]
        rows = self._dedup_media(rows)[:k]
        pairs = [
            (row["platform"], row["conversation_id"])
            for row in rows
            if row["conversation_id"]
        ]
        meta = self.state.store().conversation_meta(pairs) if pairs else {}
        media = []
        for row in rows:
            path = row["path"] or ""
            filename = path.rsplit("/", 1)[-1]
            conversation_id = row["conversation_id"] or None
            info = meta.get((row["platform"], conversation_id)) or {}
            media.append(
                {
                    "kind": row["kind"],
                    "url": f"/media/{row['platform']}/{filename}" if filename else "",
                    "platform": row["platform"],
                    "conversation_id": conversation_id,
                    "message_id": row["message_id"] or None,
                    "title": info.get("title") or "",
                    "texte": row["texte"] or "",
                    "score": round(row["score"], 4),
                    "path": path,
                }
            )
        return media

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
        config=config,
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
