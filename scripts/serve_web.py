#!/usr/bin/env python3
"""Serveur de la page de recherche dans les conversations.

Sert la page web (web/) et une petite API JSON :
  - GET /login
    (page de connexion — seule page servie sans session)
  - POST /api/login {"password": "..."}
    (connexion : pose le cookie de session signe)
  - POST /api/logout
    (deconnexion : efface le cookie)
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

Authentification : par defaut le serveur exige une session (cookie signe HMAC,
pose apres saisie du mot de passe). Le hash scrypt du mot de passe est lu dans
`secrets/web_password.txt` (cree par `scripts/web_password.py --generate`) et le
secret de signature dans `secrets/web_session_secret` ; les deux fichiers sont
locaux et non versionnes. `--no-auth` desactive la verification (local seulement).

    .venv/bin/python scripts/serve_web.py
    .venv/bin/python scripts/serve_web.py --port 9000 --host 0.0.0.0

En production, le serveur reste sur 127.0.0.1 et Tailscale publie l'acces :
voir `deploy/aicv-web.service` et la section « Mise en production » du README.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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
from src.utils.webauth import (  # noqa: E402
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    LoginThrottle,
    load_or_create_secret,
    make_token,
    read_password_file,
    session_key,
    token_parts,
    verify_password,
    verify_token,
)

WEB_DIR = ROOT / "web"
#: secrets locaux non versionnes (hash du mot de passe, cle de session)
SECRETS_DIR = ROOT / "secrets"
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
#: resultats d'une recherche (defaut cote client : 30, borne haute)
K_MAX = 100
#: longueur maximale de la requete de recherche avant encodage (cout CPU)
MAX_QUERY_CHARS = 512
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
#: pieces jointes affichables dans le navigateur (les autres sont telechargees)
INLINE_FILE_EXT = {
    ".pdf", ".txt", ".md", ".csv", ".json", ".xml",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".svg",
    ".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac",
    ".mp4", ".webm", ".mov",
}
#: extensions executables/rendues : jamais servies avec leur type d'origine
FORCE_TEXT_EXT = {
    ".js", ".mjs", ".cjs", ".css", ".html", ".htm", ".xhtml",
    ".xsl", ".xslt",
}
#: proxys de confiance pour X-Forwarded-For/Proto (boucle locale : Tailscale)
TRUSTED_PROXIES = ("127.0.0.1", "::1")
#: requetes simultanees servies au maximum (au-dela : connexion fermee)
MAX_WORKERS = 32
#: connexions simultanees par client (un pair ne peut pas monopoliser le pool)
MAX_PER_CLIENT = 8
#: corps de requete accepte (les endpoints d'action envoient < 1 Kio)
MAX_BODY = 64 * 1024
#: duree totale maximale pour recevoir le corps d'une requete
BODY_DEADLINE = 10.0
#: ressources servies sans session : la page de connexion et ses scripts
OPEN_PATHS = ("/login", "/login.js", "/theme-boot.js")
#: chemin interne accepte comme destination apres connexion (pas de controle)
NEXT_PATH_RE = re.compile(r"^/[A-Za-z0-9._~:/%?&=+@!$,;'()*\-]*$")
#: politique de securite du contenu de l'application (CDN autorises par SRI)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    # bibliotheques servies localement (web/vendor) : aucun script tiers
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'"
)
#: medias (SVG, PDF...) : document isole, sans script ni origine
MEDIA_SECURITY_POLICY = "default-src 'none'; sandbox"
#: en-tetes de fonctionnalites navigateur
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
    "interest-cohort=()"
)


def media_content_key(path: Path) -> tuple[int, str] | None:
    """Cle de contenu d'un media (taille + md5 tete/queue) pour dedoublonner.

    Le meme media re-uploade dans plusieurs conversations produit des fichiers
    identiques (chemins differents) : la cle les regroupe. Un fichier illisible
    rend None (l'item est conserve tel quel).
    """
    if path.is_symlink() or not _regular_file(path):
        return None
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


def safe_next(path: str) -> str:
    """Destination interne apres connexion (jamais une URL externe)."""
    if not path or path.startswith("//") or not NEXT_PATH_RE.match(path):
        return "/"
    return path


def _as_int(value, default: int = 0) -> int:
    """Entier force (les rapports relus d'un fichier ne sont pas fiables)."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value, default=None):
    """Flottant fini force (None si la valeur n'est pas un nombre)."""
    import math
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def read_archive_json(path: Path, max_bytes: int = 64 * 1024 * 1024):
    """JSON de l'archive, jamais un lien/FIFO/peripherique et borne en taille.

    Un `conversation_list.json` symlinke (ou un FIFO) peut faire lire hors
    archive ou bloquer indefiniment un worker : on n'accepte qu'un fichier
    regulier, sans lien dur, de taille raisonnable.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        if stat.st_nlink != 1 or stat.st_size > max_bytes:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None


def _normalize_message(message: dict) -> dict:
    """Tour de conversation : types attendus par le front (listes/chaines)."""
    out = dict(message)
    for key in ("code_blocks", "variants", "artifacts", "sources", "tools"):
        if not isinstance(out.get(key), list):
            out[key] = []
    for key in ("role", "texte", "model", "timestamp", "message_id"):
        if key in out and not isinstance(out[key], str):
            out[key] = _text(out[key])
    return out


def _text(value) -> str:
    """Texte sur (une base piegee peut contenir un BLOB a la place)."""
    return value if isinstance(value, str) else ""


def _regular_file(path: Path) -> bool:
    """Fichier regulier, sans lien dur (qui pourrait sortir de l'archive)."""
    try:
        stat = path.stat()
    except OSError:
        return False
    return stat.st_nlink == 1 and path.is_file()


def safe_url(url, platform: str, conversation_id: str) -> str:
    """URL d'origine : http(s) uniquement, sinon le lien canonique.

    Une archive piegee peut contenir `javascript:` ou `//evil` : ces valeurs
    ne doivent jamais finir dans un `href`.
    """
    if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
        return url
    return conversation_url(platform, conversation_id)


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


class AuthConfig:
    """Authentification de l'interface : mot de passe + sessions signees."""

    def __init__(
        self,
        password_hash: str,
        secret: bytes,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        cookie_secure: str = "auto",
        throttle: LoginThrottle | None = None,
    ):
        self.password_hash = password_hash
        self.ttl_seconds = max(60, int(ttl_seconds))
        #: `auto` (Secure si la requete arrive en HTTPS), `always`, `never`
        self.cookie_secure = cookie_secure
        self.throttle = throttle if throttle is not None else LoginThrottle()
        #: cle derivee du secret **et** du hash : changer le mot de passe
        #: invalide toutes les sessions, sans etat stocke
        self.key = session_key(secret, password_hash)
        #: nonces deconnectes -> expiration (revoque un jeton avant son echeance)
        self.revoked: dict[str, float] = {}
        self._revoked_lock = threading.Lock()

    def check_password(self, password: str) -> bool:
        """Mot de passe correct ?"""
        return verify_password(password, self.password_hash)

    def token(self) -> str:
        """Jeton de session signe, valable `ttl_seconds`."""
        return make_token(self.key, self.ttl_seconds)

    def valid(self, token: str | None) -> bool:
        """Jeton de session encore valide (et non deconnecte) ?"""
        if not verify_token(token, self.key):
            return False
        parts = token_parts(token)
        if parts is None:
            return False
        nonce, _ = parts
        with self._revoked_lock:
            self._purge_revoked()
            return nonce not in self.revoked

    def revoke(self, token: str | None) -> None:
        """Invalide un jeton avant son expiration (deconnexion)."""
        parts = token_parts(token)
        if parts is None:
            return
        nonce, expires = parts
        with self._revoked_lock:
            self._purge_revoked()
            if len(self.revoked) >= 4096:
                # evince les jetons qui expirent le plus tot (les moins utiles)
                for nonce, _ in sorted(
                    self.revoked.items(), key=lambda item: item[1]
                )[:512]:
                    self.revoked.pop(nonce, None)
            self.revoked[nonce] = float(expires)

    def _purge_revoked(self) -> None:
        """Oublie les nonces deja expires."""
        now = time.time()
        for nonce in [n for n, exp in self.revoked.items() if exp <= now]:
            self.revoked.pop(nonce, None)


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
        # serialise l'inference du modele (le store sqlite est deja serialise)
        self.embed_lock = threading.Lock()
        #: controle de parseur lance depuis la page de test (un seul a la fois)
        self.recheck_job = RecheckJob()
        #: scrape a la demande lance depuis la page (un seul a la fois)
        self.scrape_job = ScrapeJob()

    def store(self) -> VectorStore:
        with self._lock:
            if self._store is None:
                self._store = VectorStore(self.db_path, dim=self.dim)
            return self._store

    def platform_dir(self, platform: str) -> Path | None:
        """Dossier d'une plateforme, jamais un lien symbolique hors archive."""
        if platform not in PLATFORMS:
            return None
        root = self.exports_dir.resolve()
        base = self.exports_dir / platform
        if base.is_symlink():
            return None
        try:
            resolved = base.resolve()
        except (OSError, ValueError):
            return None
        if resolved.parent != root or not resolved.is_dir():
            return None
        return resolved

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
        with self.embed_lock:
            return self.embedder().encode_one(text)

    def close_thread_dbs(self) -> None:
        """Ferme les connexions SQLite du thread courant (fin de requete).

        Sans cela, chaque connexion HTTP laisse une connexion SQLite ouverte
        jusqu'au passage du ramasse-miettes (les descripteurs s'accumulent).
        """
        for store in (self._store, self._media_store):
            closer = getattr(store, "close_thread_db", None)
            if closer is not None:
                closer()

    def conversation_file(self, platform: str, conversation_id: str):
        # defense en profondeur : jamais de chemin hors de exports/<plateforme>
        if platform not in PLATFORMS:
            return None
        base = self.platform_dir(platform)
        if base is None:
            return None
        store = self.store()
        path = store.conversation_file(platform, conversation_id)
        if path:
            # le chemin vient de la base : on verifie son confinement avant lecture
            candidate = Path(path).resolve()
            if base in candidate.parents and _regular_file(candidate):
                return candidate
        for candidate in base.glob("*.json"):
            if candidate.name == "conversation_list.json":
                continue
            # un lien symbolique peut pointer hors de l'archive : on resout et
            # on exige le confinement, comme pour le chemin venu de la base
            if candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve()
            except (OSError, ValueError, RecursionError):
                continue
            if base not in resolved.parents or not _regular_file(resolved):
                continue
            data = read_archive_json(resolved)
            if not isinstance(data, dict):
                continue
            if str(data.get("conversation_id")) == str(conversation_id):
                return resolved
        return None


class WebServer(ThreadingHTTPServer):
    """Serveur multi-thread borne : au plus `max_workers` requetes en cours.

    Sans borne, quelques centaines de connexions lentes (slowloris) suffisent a
    epuiser les threads ; ici la connexion en trop est fermee immediatement.
    Le quota **par client** est tenu par le handler, une fois les en-tetes lus :
    derriere un proxy, tous les clients partagent la meme adresse de pair, donc
    seule la cle issue de `X-Forwarded-For` distingue les clients reels.
    """

    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, max_workers: int = MAX_WORKERS,
                 max_per_client: int = MAX_PER_CLIENT):
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_workers)
        self.max_per_client = max_per_client
        self._per_client: dict[str, int] = {}
        self._client_lock = threading.Lock()

    def claim_client(self, key: str) -> bool:
        """Reserve une place pour ce client reel (False si quota atteint)."""
        with self._client_lock:
            count = self._per_client.get(key, 0)
            if count >= self.max_per_client:
                return False
            self._per_client[key] = count + 1
            return True

    def release_client(self, key: str) -> None:
        with self._client_lock:
            count = self._per_client.get(key, 0) - 1
            if count > 0:
                self._per_client[key] = count
            else:
                self._per_client.pop(key, None)

    def process_request(self, request, client_address):
        # quota global de threads : la connexion en trop est fermee. Le quota
        # par client reel est tenu par le handler (apres les en-tetes) ; la
        # phase pre-headers n'est atteignable que depuis la machine elle-meme
        # (le proxy Tailscale lit les en-tetes avant de relayer).
        if not self._slots.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "AICV"
    sys_version = ""
    #: HTTP/1.1 : connexions persistantes (Content-Length toujours envoye)
    protocol_version = "HTTP/1.1"
    #: ferme les connexions muettes (slowloris)
    timeout = 15
    state: AppState = None  # injecte au demarrage
    auth: AuthConfig | None = None  # None = pas d'authentification
    _head_only = False
    _error_response = False

    # -- helpers ---------------------------------------------------------------

    def _is_https(self) -> bool:
        """Requete arrivee en HTTPS (TLS direct ou proxy Tailscale) ?"""
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            return True
        return isinstance(self.connection, ssl.SSLSocket)

    def _security_headers(self, sandbox: bool = False) -> None:
        """En-tetes de durcissement communs a toutes les reponses."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", PERMISSIONS_POLICY)
        self.send_header(
            "Content-Security-Policy",
            MEDIA_SECURITY_POLICY if sandbox else CONTENT_SECURITY_POLICY,
        )
        if self._is_https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _send_json(self, payload, status: int = 200) -> None:
        # ensure_ascii : un surrogate isole (archive piegee) ne doit pas
        # faire echouer l'encodage de la reponse
        body = json.dumps(payload, ensure_ascii=True, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        if not self._head_only:
            self.wfile.write(body)

    def _send_file(self, path: Path, cache: str = "no-cache",
                   sandbox: bool = False, attachment: bool = False,
                   content_type: str | None = None) -> None:
        try:
            handle = path.open("rb")
            stat = os.fstat(handle.fileno())
            if not stat.st_size and not path.is_file():
                raise OSError("fichier non regulier")
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        with handle:
            last_modified = formatdate(stat.st_mtime, usegmt=True)
            if self._not_modified(stat.st_mtime):
                self.send_response(304)
                self.send_header("Last-Modified", last_modified)
                self.send_header("Cache-Control", cache)
                self._security_headers(sandbox)
                self.end_headers()
                return
            content_type = content_type or CONTENT_TYPES.get(
                path.suffix, "application/octet-stream"
            )
            if content_type == "application/octet-stream":
                # telechargements sans extension fiable (.img) : type devine
                content_type = sniff_image_type(handle.read(512)) or content_type
                handle.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", cache)
            if attachment:
                # jamais execute/rendu same-origin (.js, .html, .css...)
                self.send_header("Content-Disposition", "attachment")
            self._security_headers(sandbox)
            self.end_headers()
            if self._head_only:
                return
            # envoi par blocs, **borne a la taille annoncee** : un fichier dont
            # la taille change (procfs, fichier en cours d'ecriture) ne doit pas
            # deborder sur la reponse suivante d'une connexion keep-alive
            remaining = stat.st_size
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    # fichier tronque en cours d'envoi : la reponse est plus
                    # courte que Content-Length, on ferme la connexion
                    self.close_connection = True
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _not_modified(self, mtime: float) -> bool:
        """`If-Modified-Since` couvre la version du fichier (reponse 304) ?"""
        raw = self.headers.get("If-Modified-Since")
        if not raw:
            return False
        try:
            since = parsedate_to_datetime(raw).timestamp()
        except (TypeError, ValueError, AttributeError, OverflowError):
            return False
        return int(mtime) <= int(since)

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()

    def send_error(self, code, message=None, explain=None):
        """Erreur du handler de base : statut toujours ecrit + en-tetes."""
        # send_response_only() n'ecrit rien quand request_version vaut HTTP/0.9
        # (cas d'un echec de parsing) : on retablit une version avant l'envoi.
        if getattr(self, "request_version", "HTTP/0.9") == "HTTP/0.9":
            self.request_version = "HTTP/1.1"
        # un echec de parsing laisse `command` a None : on relit la ligne brute
        # pour ne jamais renvoyer de corps a un HEAD
        line = getattr(self, "raw_requestline", b"") or b""
        if line[:5].upper() == b"HEAD ":
            self.command = "HEAD"
            self._head_only = True
        self._error_response = True
        try:
            super().send_error(code, message, explain)
        finally:
            self._error_response = False

    def end_headers(self) -> None:
        # apres un echec de parsing (505/414), la classe de base laisse
        # request_version a HTTP/0.9 et n'ecrit alors NI statut NI en-tetes :
        # on force l'ecriture pour que le client recoive une vraie reponse.
        if self.request_version == "HTTP/0.9" and getattr(self, "_headers_buffer", None):
            self.request_version = "HTTP/1.1"
        if self._error_response:
            try:
                self._security_headers()
            except Exception:  # noqa: BLE001 - requete trop malformee
                pass
        buffer = getattr(self, "_headers_buffer", None)
        if buffer is not None and self.close_connection and not any(
            b"connection:" in line.lower() for line in buffer
        ):
            # on annonce la fermeture : un client HTTP/1.1 ne doit pas reutiliser
            # une connexion que le serveur va fermer (413, 400, 408, 405...)
            self.send_header("Connection", "close")
        super().end_headers()

    def version_string(self) -> str:
        """`Server:` sans version de Python."""
        return self.server_version

    def log_request(self, code="-", size="-"):
        """Journal sans query string ni caractere de controle.

        Les recherches ne finissent pas dans le journal ; une requete malformee
        (pas de `self.path`) ne doit pas lever d'exception.
        """
        raw = getattr(self, "path", "") or "/"
        try:
            path = urlparse(raw).path or "/"
        except ValueError:
            path = "/"
        path = "".join(ch for ch in path if 32 <= ord(ch) < 127)[:200]
        command = getattr(self, "command", None) or "-"
        command = "".join(ch for ch in str(command) if 32 <= ord(ch) < 127)[:40]
        sys.stderr.write(f'[web] "{command} {path}" {code} {size}\n')

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[web] %s\n" % (fmt % args))

    # -- authentification ------------------------------------------------------

    def _client_key(self) -> str:
        """Cle de limitation des tentatives (IP reelle derriere le proxy).

        `X-Forwarded-For` n'est cru que si la connexion vient d'un proxy de
        confiance (la boucle locale, ici Tailscale) ; on prend le **dernier**
        saut, celui ajoute par le proxy, jamais la valeur fournie par le client.
        """
        peer = self.client_address[0] if self.client_address else "?"
        if peer in TRUSTED_PROXIES:
            hops = [
                hop.strip()
                for hop in (self.headers.get("X-Forwarded-For") or "").split(",")
                if hop.strip()
            ]
            if hops:
                return hops[-1]
        return peer

    def _cookie_secure(self) -> bool:
        mode = self.auth.cookie_secure if self.auth else "never"
        if mode == "always":
            return True
        if mode == "never":
            return False
        return self._is_https()

    def _session_cookie(self, token: str, max_age: int) -> str:
        parts = [
            f"{SESSION_COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={max_age}",
        ]
        if self._cookie_secure():
            parts.append("Secure")
        return "; ".join(parts)

    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        for chunk in raw.split(";"):
            name, _, value = chunk.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _authenticated(self) -> bool:
        """Session valide (ou authentification desactivee) ?"""
        if self.auth is None:
            return True
        return self.auth.valid(self._session_token())

    def _require_auth(self, path: str, *, page: bool = False) -> bool:
        """Verifie la session ; repond et renvoie False si l'acces est refuse."""
        if self._authenticated():
            return True
        if page:
            self._redirect(f"/login?next={quote(safe_next(path))}")
        else:
            self._send_json({"error": "authentification requise", "login": "/login"}, 401)
        return False

    def _check_origin(self) -> bool:
        """Refuse un POST venu d'un autre site (anti-CSRF, en plus de SameSite)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # client non navigateur (curl, tests)
        host = self.headers.get("Host") or ""
        try:
            parsed = urlparse(origin)
        except ValueError:
            self._send_json({"error": "origine refusee"}, 403)
            return False
        if parsed.netloc.lower() != host.lower():
            self._send_json({"error": "origine refusee"}, 403)
            return False
        # derriere le proxy HTTPS, une origine http:// est refusee aussi
        if self._is_https() and parsed.scheme != "https":
            self._send_json({"error": "origine refusee"}, 403)
            return False
        return True

    def _login(self, body) -> None:
        """Connexion : mot de passe -> cookie de session signe."""
        if self.auth is None:
            self._send_json({"ok": True, "auth": False})
            return
        key = self._client_key()
        wait = self.auth.throttle.retry_after(key)
        if wait:
            self._send_json(
                {"error": f"trop de tentatives, reessayer dans {wait} s"}, 429
            )
            return
        password = body.get("password")
        if not isinstance(password, str) or not password:
            self._send_json({"error": "mot de passe requis"}, 400)
            return
        if not self.auth.check_password(password):
            locked = self.auth.throttle.fail(key)
            message = "mot de passe incorrect"
            if locked:
                message = f"mot de passe incorrect — bloque {locked} s"
            self._send_json({"error": message}, 401)
            return
        self.auth.throttle.reset(key)
        token = self.auth.token()
        payload = json.dumps(
            {"ok": True, "ttl": self.auth.ttl_seconds, "secure": self._cookie_secure()}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", self._session_cookie(token, self.auth.ttl_seconds))
        self._security_headers()
        self.end_headers()
        if not self._head_only:
            self.wfile.write(payload)

    def _logout(self) -> None:
        """Deconnexion : revoque le jeton et efface le cookie."""
        if self.auth is not None:
            # le jeton reste signe valable jusqu'a expiration : on le revoque
            self.auth.revoke(self._session_token())
        self.send_response(200)
        payload = b'{"ok": true}'
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", self._session_cookie("", 0))
        self._security_headers()
        self.end_headers()
        if not self._head_only:
            self.wfile.write(payload)

    # -- routes ----------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            # tout corps annonce est consomme (ou la connexion fermee) AVANT
            # toute reponse : jamais de bytes laisses pour la requete suivante
            if not self._claim_client():
                self.close_connection = True
                self._send_json({"error": "trop de connexions simultanees"}, 503)
                return
            self._read_request_body()
            if path in OPEN_PATHS:
                # page de connexion et ses scripts : servis sans session
                if path == "/login" and self.auth is not None and self._authenticated():
                    self._redirect("/")
                    return
                name = "login.html" if path == "/login" else path.lstrip("/")
                self._send_file(WEB_DIR / name)
                return
            if not self._require_auth(path, page=not path.startswith(("/api/", "/media/"))):
                return
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
        except ConnectionResetError:
            pass
        except ValueError:
            # cible de requete illisible (URI absolue, IPv6 invalide...)
            self._send_json({"error": "requete invalide"}, 400)
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except Exception:  # noqa: BLE001
            # jamais de detail interne au client (chemins, versions, modeles)
            traceback.print_exc()
            self._send_json({"error": "erreur interne"}, 500)

    def do_HEAD(self) -> None:  # noqa: N802
        """Repond aux en-tetes sans corps (pages et fichiers uniquement)."""
        self._head_only = True  # avant toute lecture : une erreur n'aura pas de corps
        try:
            # quota par client AVANT la lecture du corps : un HEAD avec un corps
            # lent ne doit pas contourner le plafond de connexions
            if not self._claim_client():
                self.close_connection = True
                self._send_json({"error": "trop de connexions simultanees"}, 503)
                return
            self._read_request_body()
            if urlparse(self.path).path.startswith("/api/"):
                # un HEAD sur /api lancerait la requete complete pour rien
                self._send_json({"error": "HEAD non supporte sur /api"}, 405)
                return
            self.do_GET()
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except ValueError:
            self._send_json({"error": "requete invalide"}, 400)
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": "erreur interne"}, 500)
        finally:
            self._head_only = False

    def _method_not_allowed(self) -> None:
        """Methodes non servies : 405 propre, sans laisser de corps dans le socket."""
        self.close_connection = True
        self._send_json({"error": "methode non supportee"}, 405)

    do_OPTIONS = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_TRACE = _method_not_allowed

    def do_POST(self) -> None:  # noqa: N802
        """Endpoints d'action (connexion, scrape, controle des parseurs)."""
        try:
            path = urlparse(self.path).path
            if not self._claim_client():
                self.close_connection = True
                self._send_json({"error": "trop de connexions simultanees"}, 503)
                return
            self._read_request_body()
            if path == "/api/login":
                if self._check_origin():
                    self._login(self._read_json_body())
                return
            if not path.startswith("/api/"):
                self._send_json({"error": "not found"}, 404)
                return
            if not self._require_auth(path):
                return
            if not self._check_origin():
                return
            if path == "/api/logout":
                self._logout()
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
        except ConnectionResetError:
            pass
        except ValueError:
            self._send_json({"error": "requete invalide"}, 400)
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": "erreur interne"}, 500)

    def handle_one_request(self) -> None:
        """Traite une requete : remet l'etat par requete a zero.

        L'instance de handler est **reutilisee** pour toutes les requetes d'une
        connexion persistante : sans cette remise a zero, le corps de la
        premiere requete serait rejoue (login casse) ou ignore (corps laisse
        dans le socket -> desynchronisation).
        """
        self._raw_body = None
        self._head_only = False
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            # client parti pendant la lecture des en-tetes : pas de traceback
            self.close_connection = True

    def log_error(self, fmt, *args):  # noqa: A003
        """Journal d'erreur tronque (une ligne de requete peut faire 64 Kio)."""
        try:
            text = fmt % args
        except Exception:  # noqa: BLE001
            text = str(fmt)
        sys.stderr.write("[web] %s\n" % text[:300])

    def _claim_client(self) -> bool:
        """Quota de connexions simultanees par client reel (une fois par connexion).

        La cle est `_client_key()` (derniere adresse reelle derriere le proxy) et
        non l'adresse du pair : sinon, derriere Tailscale Serve, tous les clients
        partageraient le meme compteur et huit connexions suffiraient a fermer
        l'interface.
        """
        if getattr(self, "_client_claimed_key", None) is not None:
            return True
        if getattr(self, "_client_claim_refused", False):
            return False
        claim = getattr(self.server, "claim_client", None)
        if claim is None:  # serveur sans quota (tests)
            return True
        key = self._client_key()
        if claim(key):
            self._client_claimed_key = key
            return True
        self._client_claim_refused = True
        return False

    def finish(self) -> None:
        """Fin de connexion : relache le quota client et les connexions SQLite."""
        key = getattr(self, "_client_claimed_key", None)
        try:
            if key is not None:
                release = getattr(self.server, "release_client", None)
                if release is not None:
                    release(key)
            state = self.state
            if state is not None:
                try:
                    state.close_thread_dbs()
                except Exception:  # noqa: BLE001 - fermeture best effort
                    pass
        finally:
            super().finish()

    def _body_length(self) -> int:
        """Longueur annoncee du corps (0 si absent), refus si non supporte."""
        if (self.headers.get("Transfer-Encoding") or "").strip():
            # on ne decode pas le chunked : jamais de bytes non comprimes laisses
            self.close_connection = True
            raise ApiError("transfer-encoding non supporte", 400)
        values = self.headers.get_all("Content-Length") or []
        if len(values) > 1:
            # deux longueurs = desynchronisation possible selon l'intermediaire
            self.close_connection = True
            raise ApiError("content-length duplique", 400)
        raw = (values[0] if values else "").strip()
        if not raw:
            return 0
        if not raw.isascii() or not raw.isdigit() or len(raw) > 10:
            # chiffres non decimaux, valeur absurde ou plus longue que MAX_BODY
            self.close_connection = True
            raise ApiError("content-length invalide", 400)
        try:
            return int(raw)
        except ValueError as exc:  # limite CPython d'entiers
            self.close_connection = True
            raise ApiError("content-length invalide", 400) from exc

    def _read_exact(self, length: int) -> bytes:
        """Lit exactement `length` octets, avec une echeance **totale**.

        Le timeout du handler est un timeout par `recv` : un octet toutes les
        5 s maintiendrait la connexion indefiniment. Ici la duree totale est
        bornee (anti-slowloris sur le corps, qui traverse le proxy).
        """
        deadline = time.monotonic() + BODY_DEADLINE
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    self.close_connection = True
                    raise ApiError("corps trop lent", 408)
                self.connection.settimeout(min(left, 5.0))
                try:
                    chunk = self.rfile.read(min(remaining, 64 * 1024))
                except (TimeoutError, OSError) as exc:
                    # silence du client : echeance par recv atteinte
                    self.close_connection = True
                    raise ApiError("corps trop lent", 408) from exc
                if not chunk:
                    self.close_connection = True
                    raise ApiError("corps incomplet", 400)
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            try:
                self.connection.settimeout(self.timeout)
            except OSError:
                pass
        return b"".join(chunks)

    def _read_request_body(self) -> bytes:
        """Corps de la requete, lu **une seule fois** (memoise).

        Tout corps annonce est consomme ou la connexion est fermee : sinon les
        octets restants seraient interpretes comme la requete suivante
        (desynchronisation de file derriere un proxy mutualisant).
        """
        raw = getattr(self, "_raw_body", None)
        if raw is not None:
            return raw
        length = self._body_length()
        if length <= 0:
            self._raw_body = b""
            return self._raw_body
        if length > MAX_BODY:
            self.close_connection = True
            self._raw_body = b""
            raise ApiError("corps trop volumineux", 413)
        self._raw_body = self._read_exact(length)
        return self._raw_body

    def _read_json_body(self) -> dict:
        """Corps JSON de la requete (objet vide si absent)."""
        raw = self._read_request_body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            # ValueError couvre JSONDecodeError, les octets non UTF-8 ET la
            # limite CPython d'entiers (4300 chiffres) ; RecursionError le
            # JSON trop profond
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
        base = self.state.platform_dir(platform)
        if base is None:
            self._send_json({"error": "not found"}, 404)
            return
        for subdir in ("images", "files"):
            try:
                sub = base / subdir
                # un sous-dossier symlinke (images -> ../../secrets) ferait
                # de `root` la cible : il doit etre un vrai dossier enfant
                if sub.is_symlink() or not sub.is_dir():
                    continue
                root = sub.resolve()
                if root.parent != base:
                    continue
                candidate = (root / filename).resolve()
                inside = root in candidate.parents and candidate.is_file()
                # un lien dur peut pointer vers un fichier hors archive :
                # seuls les fichiers ecrits par le scrapeur (nlink=1) sont servis
                hardlink = inside and candidate.stat().st_nlink > 1
            except (OSError, ValueError, RecursionError):
                # nom de fichier absurde (NUL, composant trop long...)
                continue
            if inside and not hardlink:
                # noms de fichiers adresses par leur hash : cache long
                # `sandbox` : SVG/HTML directement ouverts ne peuvent rien executer
                suffix = candidate.suffix.lower()
                download = subdir == "files" and suffix not in INLINE_FILE_EXT
                self._send_file(
                    candidate, cache="private, max-age=86400", sandbox=True,
                    attachment=download,
                    content_type=(
                        "text/plain; charset=utf-8"
                        if suffix in FORCE_TEXT_EXT else None
                    ),
                )
                return
        self._send_json({"error": "not found"}, 404)

    def _serve_static(self, path: str) -> None:
        try:
            relative = path.lstrip("/")
            candidate = (WEB_DIR / relative).resolve()
            exists = candidate.is_file()
        except (OSError, ValueError, RecursionError):
            self._send_json({"error": "not found"}, 404)
            return
        if WEB_DIR not in candidate.parents and candidate != WEB_DIR:
            self._send_json({"error": "forbidden"}, 403)
            return
        if not exists:
            self._send_json({"error": "not found"}, 404)
            return
        self._send_file(candidate)

    def _handle_api(self, path: str, params) -> None:
        # pas de verrou global : SQLite est serialise et l'inference a le sien,
        # sinon une recherche lente bloquerait /api/stats et toute l'interface
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
            if row["platform"] in PLATFORMS
        }
        convs = {
            row["platform"]: row["n"]
            for row in store.db.execute(
                "SELECT platform, COUNT(*) AS n FROM conversations GROUP BY platform"
            ).fetchall()
            if row["platform"] in PLATFORMS
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
            if row["platform"] not in PLATFORMS:
                continue
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
        model_series.sort(
            key=lambda item: (-item["total"], str(item["model"]))
        )
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
                if not 0 <= offset <= 10**9:
                    raise ApiError("i doit etre entre 0 et 1000000000")
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
        if offset > 10**9:
            raise ApiError("offset doit etre inferieur a 1000000000")
        if not 1 <= limit <= MEDIA_PAGE_MAX:
            raise ApiError(f"limit doit etre entre 1 et {MEDIA_PAGE_MAX}")
        store = self.state.media_store()
        # liste blanche de plateformes : une base piegee ne fait pas remonter
        # des lignes hors perimetre dans la galerie
        clauses = [f"platform IN ({','.join('?' for _ in PLATFORMS)})"]
        args = list(PLATFORMS)
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
        per_platform = {
            name: _as_int(count)
            for name, count in (index.get("per_platform") or {}).items()
            if name in PLATFORMS
        }
        index["per_platform"] = per_platform
        index["messages"] = sum(per_platform.values())
        index["conversations"] = sum(
            _as_int(row["n"])
            for row in store.db.execute(
                "SELECT platform, COUNT(*) AS n FROM conversations "
                "GROUP BY platform"
            ).fetchall()
            if row["platform"] in PLATFORMS
        )
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
            base = self.state.platform_dir(name)
            if base is None:
                continue
            path = base / "conversation_list.json"
            entry = {
                "platform": name,
                "conversations": 0,
                "last_message_at": None,
                "scraped_at": None,
                "health": self._api_health(name),
                "recheck": self._recheck(name),
            }
            data = read_archive_json(path)
            if data is not None:
                entries = [
                    item for item in (
                        data.get("conversations") if isinstance(data, dict) else []
                    ) or []
                    if isinstance(item, dict)
                ]
                entry["conversations"] = len(entries)
                stamps = [
                    item.get("last_message_at") for item in entries
                    if isinstance(item.get("last_message_at"), str)
                    and item.get("last_message_at")
                ]
                entry["last_message_at"] = max(stamps) if stamps else None
                try:
                    entry["scraped_at"] = datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                except (OSError, ValueError, OverflowError):
                    entry["scraped_at"] = None
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
        base = self.state.platform_dir(platform)
        if base is None:
            return None
        data = read_archive_json(base / "api_health.json")
        if not isinstance(data, dict):
            return None
        n = _as_int(data.get("n"))
        matches = _as_int(data.get("matches"))
        return {
            "n": n,
            "matches": matches,
            "rate": round(matches / n * 100, 1) if n else None,
            "breaks": _as_int(data.get("breaks")),
            "drifts": _as_int(data.get("drifts")),
            "mean_score": _as_float(data.get("mean_score")),
            "threshold": _as_float(data.get("threshold")),
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
            # dedoublonne : un corps borne peut contenir des milliers de
            # fois la meme plateforme (amplification des jobs)
            platforms = list(
                dict.fromkeys(check_platform(str(name)) for name in raw)
            )
        else:
            raise ApiError("platforms doit etre une liste")
        if not platforms:
            raise ApiError("choisir au moins une plateforme")
        try:
            per_platform = int(body.get("per_platform", PER_PLATFORM))
            min_messages = int(body.get("min_messages", MIN_MESSAGES))
            seed = int(body.get("seed", 0))
        except (TypeError, ValueError, OverflowError) as exc:
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
            # dedoublonne : un corps borne peut contenir des milliers de
            # fois la meme plateforme (amplification des jobs)
            platforms = list(
                dict.fromkeys(check_platform(str(name)) for name in raw)
            )
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
            except (TypeError, ValueError, OverflowError) as exc:
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
            SELECT message_id, conversation_id, platform, role, model, timestamp,
                   CASE WHEN typeof(texte) = 'text' THEN texte ELSE '' END AS texte
            FROM messages WHERE date(timestamp) = ?
            ORDER BY timestamp, rowid
            """,
            (date,),
        ).fetchall()
        groups: dict = {}
        order: list = []
        for row in rows:
            if row["platform"] not in PLATFORMS:
                continue
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
            if isinstance(info.get("url"), str) and info["url"].lower().startswith(
                ("http://", "https://")
            ):
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
            name for name in PLATFORMS
            if self.state.platform_dir(name) is not None
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
        # borne AVANT l'encodage : une requete de plusieurs Ko coute des dizaines
        # de secondes d'inference et bloque les recherches concurrentes
        query = query[:MAX_QUERY_CHARS]
        try:
            k = int((params.get("k") or ["30"])[0])
        except ValueError as exc:
            raise ApiError("parametre k invalide (entier attendu)") from exc
        try:
            alpha = float((params.get("alpha") or ["0.6"])[0])
        except ValueError as exc:
            raise ApiError("parametre alpha invalide (nombre attendu)") from exc
        if not 1 <= k <= K_MAX:
            raise ApiError(f"k doit etre entre 1 et {K_MAX}")
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
        # une base/copie d'archive peut contenir une plateforme inconnue : on
        # ne la renvoie jamais (liste blanche, comme les autres routes).
        # `rowid` (identifiant interne de la base) n'est pas expose par l'API web.
        results = [
            {key: value for key, value in item.items() if key != "rowid"}
            for item in results
            if item.get("platform") in PLATFORMS
        ]
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
            if row["platform"] not in PLATFORMS:
                continue
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
            if isinstance(info.get("url"), str) and info["url"].lower().startswith(
                ("http://", "https://")
            ):
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
            if isinstance(info.get("url"), str) and info["url"].lower().startswith(
                ("http://", "https://")
            ):
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
            if row.get("platform") not in PLATFORMS:
                continue
            key = (row.get("platform"), row.get("conversation_id"))
            if key in seen:
                continue
            seen.add(key)
            row["url"] = safe_url(
                row.get("url"), row["platform"], row["conversation_id"]
            )
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
            base = self.state.platform_dir(name)
            if base is None:
                continue
            path = base / "conversation_list.json"
            if not path.exists():
                continue
            data = read_archive_json(path)
            if data is None:
                continue
            entries = (
                data.get("conversations") if isinstance(data, dict) else []
            ) or []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                conversation_id = entry.get("conversation_id")
                items.append(
                    {
                        "platform": name,
                        "conversation_id": conversation_id,
                        "title": entry.get("title") or "",
                        "url": safe_url(entry.get("url"), name, conversation_id),
                        "message_count": _as_int(entry.get("message_count"), None),
                        "last_message_at": entry.get("last_message_at")
                        if isinstance(entry.get("last_message_at"), str)
                        else None,
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
        data = read_archive_json(path)
        if not isinstance(data, dict):
            # absent, trop volumineux, non-objet ou illisible
            return {"error": "conversation illisible"}
        if isinstance(data.get("messages"), list):
            # une archive piegee peut contenir des tours non-objet ou des
            # champs de mauvais type : le front ferait `.map` dessus
            data = {
                **data,
                "messages": [_normalize_message(m)
                             for m in data["messages"] if isinstance(m, dict)],
            }
        # le champ `platform` du fichier n'est jamais cru (il vient de
        # l'archive) : la valeur validee de la requete fait foi
        data = {**data, "platform": platform}
        return {
            "platform": platform,
            "conversation_id": conversation_id,
            "url": safe_url(data.get("url"), platform, conversation_id),
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
    parser.add_argument("--no-auth", action="store_true",
                        help="desactiver l'authentification (local uniquement)")
    parser.add_argument("--password-file", type=Path,
                        default=SECRETS_DIR / "web_password.txt",
                        help="fichier du hash scrypt du mot de passe")
    parser.add_argument("--session-secret", type=Path,
                        default=SECRETS_DIR / "web_session_secret",
                        help="fichier du secret de signature des sessions")
    parser.add_argument("--session-ttl", type=float,
                        default=SESSION_TTL_SECONDS / 3600,
                        help="duree de vie d'une session, en heures (defaut : 720)")
    parser.add_argument("--secure-cookies", choices=("auto", "always", "never"),
                        default="auto",
                        help="attribut Secure du cookie (defaut : auto = si HTTPS)")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")

    def resolve(value) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    exports_dir = resolve(args.exports or config["output_dir"])
    db_path = resolve(args.db or config["rag_db"])
    # les jobs (scrape / controle) heritent des chemins **resolus de cette
    # instance** : une instance de test ne doit jamais ecrire dans l'archive
    job_config = {
        **config,
        "output_dir": str(exports_dir),
        "rag_db": str(db_path),
    }
    Handler.state = AppState(
        db_path,
        exports_dir,
        args.model or config.get("rag_model"),
        config.get("rag_dim") or 0,
        config=job_config,
    )
    if args.no_auth and args.host not in ("127.0.0.1", "::1", "localhost"):
        parser.error("--no-auth exige une ecoute sur la boucle locale (--host 127.0.0.1)")
    if args.no_auth:
        Handler.auth = None
        print("[web] ATTENTION : authentification desactivee (--no-auth)")
    else:
        password_file = resolve(args.password_file)
        password_hash = read_password_file(password_file)
        if not password_hash:
            print(
                "[web] aucun mot de passe defini :\n"
                "      .venv/bin/python scripts/web_password.py --generate",
                file=sys.stderr,
            )
            return 2
        secret_path = resolve(args.session_secret)
        try:
            secret = load_or_create_secret(secret_path)
        except OSError as exc:
            print(
                f"[web] secret de session inaccessible ({secret_path}) : {exc}\n"
                "      creer les secrets avant de demarrer :\n"
                "      .venv/bin/python scripts/web_password.py --generate",
                file=sys.stderr,
            )
            return 2
        Handler.auth = AuthConfig(
            password_hash=password_hash,
            secret=secret,
            ttl_seconds=int(args.session_ttl * 3600),
            cookie_secure=args.secure_cookies,
        )
    server = WebServer((args.host, args.port), Handler)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}"
    etat = "mot de passe" if Handler.auth else "SANS authentification"
    print(f"[web] {url}  ({etat} ; Ctrl+C pour arreter)")
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
