"""Telechargement local des images de contenu references dans le markdown.

Les conversations contiennent des images (pieces jointes, images generees) sous
forme `![alt](https://...)`. Les URL sont souvent signees et expirantes : il faut
donc les telecharger pendant le run, avec la session authentifiee du service,
puis reecrire le markdown vers un chemin relatif `images/<hash>.<ext>`.

Le `loader` est un callable `url -> (bytes, content_type) | None` fourni par le
moteur (session navigateur) ou `http_loader` (URL publiques).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from .logging import get_logger, log_fields

log = get_logger("aicv.images")

IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
IMAGES_SUBDIR = "images"
FILES_SUBDIR = "files"
#: liens markdown `[nom](url)` dont le nom/URL a une extension de piece jointe
FILE_LINK_RE = re.compile(r"\[([^\]]{0,160})\]\((https?://[^)\s]+)\)")
FILE_EXTS = (
    ".pdf", ".csv", ".json", ".txt", ".md", ".rtf",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".zip",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac",
    ".mp4", ".mov", ".webm", ".mkv",
)

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}
_KNOWN_EXTS = tuple(sorted(set(_EXT_BY_TYPE.values())))
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ImageLoader = Callable[[str], Optional[Tuple[bytes, Optional[str]]]]


def extract_image_urls(text: str) -> List[str]:
    """URL d'images markdown, uniques, dans l'ordre d'apparition."""
    seen: Dict[str, None] = {}
    for url in IMAGE_RE.findall(text or ""):
        seen.setdefault(url, None)
    return list(seen)


def http_loader(url: str, timeout: int = 20) -> Optional[Tuple[bytes, Optional[str]]]:
    """Telechargement non authentifie (URL publiques : gemini, S3 pre-signe)."""
    try:
        request = Request(url, headers={"User-Agent": _UA, "Accept": "image/*,*/*"})
        with urlopen(request, timeout=timeout) as response:
            ctype = response.headers.get("Content-Type")
            return response.read(), ctype
    except Exception as exc:  # noqa: BLE001
        log.debug("http_loader echec %s: %s", url, exc)
        return None


def _extension(content_type: Optional[str], url: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ctype]
    path = url.split("?", 1)[0]
    for ext in _KNOWN_EXTS:
        if path.lower().endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".img"


def _is_image(data: bytes, content_type: Optional[str]) -> bool:
    """Vrai si les octets sont bien une image (evite les pages HTML/JSON)."""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
        return False
    if ctype.startswith("image/"):
        return True
    return _looks_like_image(data)


def _looks_like_image(data: bytes) -> bool:
    if not data:
        return False
    if data[:2] == b"\xff\xd8":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[:2] == b"BM":
        return True
    head = data[:256].lstrip().lower()
    return head.startswith(b"<svg") or head.startswith(b"<?xml")


def _rewrite_image_urls(text: str, mapping: Dict[str, str]) -> str:
    """Reecrit les URL d'images markdown vers leur chemin local.

    Seule la syntaxe `![alt](url)` est touchee : une meme URL citee dans la
    prose (lien utilisateur vers l'image) reste un lien web.
    """
    if not text or not mapping:
        return text

    def repl(match: "re.Match[str]") -> str:
        relpath = mapping.get(match.group(1))
        if not relpath:
            return match.group(0)
        prefix = match.group(0)[: match.start(1) - match.start(0)]
        suffix = match.group(0)[match.end(1) - match.start(0):]
        return f"{prefix}{relpath}{suffix}"

    return IMAGE_RE.sub(repl, text)


def download_conversation_images(
    conv,
    loader: ImageLoader,
    images_dir: Path,
    subdir: str = IMAGES_SUBDIR,
) -> Dict[str, int]:
    """Telecharge les images des messages et reecrit leur markdown.

    Modifie `conv.messages[*].texte` en place. Retourne des compteurs.
    """
    urls: Dict[str, None] = {}
    for message in conv.messages:
        for url in extract_image_urls(message.texte):
            urls.setdefault(url, None)
    if not urls:
        return {"downloaded": 0, "cached": 0, "failed": 0}

    images_dir = Path(images_dir)
    mapping: Dict[str, str] = {}
    stats = {"downloaded": 0, "cached": 0, "failed": 0}

    for url in urls:
        existing = _cached_file(images_dir, url)
        if existing is not None:
            mapping[url] = f"{subdir}/{existing.name}"
            stats["cached"] += 1
            continue
        result = loader(url)
        if not result:
            stats["failed"] += 1
            continue
        data, content_type = result
        if not data or not _is_image(data, content_type):
            stats["failed"] += 1
            continue
        filename = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + _extension(content_type, url)
        images_dir.mkdir(parents=True, exist_ok=True)
        try:
            (images_dir / filename).write_bytes(data)
        except OSError as exc:
            log.debug("ecriture image %s: %s", filename, exc)
            stats["failed"] += 1
            continue
        mapping[url] = f"{subdir}/{filename}"
        stats["downloaded"] += 1

    for message in conv.messages:
        message.texte = _rewrite_image_urls(message.texte, mapping)

    log_fields(
        log, 20, "images telechargees",
        extra={"platform": getattr(conv, "platform", None), **stats,
               "total": len(urls)},
    )
    return stats


_EXT_BY_MIME = {
    "application/pdf": ".pdf",
    "text/csv": ".csv",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/zip": ".zip",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
}


def _is_file_link(name: str, url: str) -> bool:
    """Vrai si le lien est une piece jointe (extension connue sur le nom/URL)."""
    path = url.split("?", 1)[0].lower()
    lowered = (name or "").lower()
    return any(
        lowered.endswith(ext) or path.endswith(ext) for ext in FILE_EXTS
    )


def _file_extension(name: str, content_type: Optional[str], url: str) -> str:
    lowered = (name or "").lower()
    for ext in FILE_EXTS:
        if lowered.endswith(ext):
            return ext
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _EXT_BY_MIME:
        return _EXT_BY_MIME[ctype]
    path = url.split("?", 1)[0].lower()
    for ext in FILE_EXTS:
        if path.endswith(ext):
            return ext
    return ".bin"


def _is_document(data: bytes, content_type: Optional[str]) -> bool:
    """Vrai si les octets sont un document/media (pas une page HTML de login)."""
    if not data:
        return False
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in ("text/html", "application/xhtml+xml"):
        return False
    head = data[:256].lstrip().lower()
    return not (head.startswith(b"<!doctype html") or head.startswith(b"<html"))


def download_conversation_files(
    conv,
    loader: ImageLoader,
    files_dir: Path,
    subdir: str = FILES_SUBDIR,
) -> Dict[str, int]:
    """Telecharge les pieces jointes (liens `[nom](url)`) et reecrit le markdown.

    Ne traite que les liens dont le nom ou l'URL a une extension de fichier
    (pdf/csv/json/txt/docx/audio/video...) pour ne pas aspirer les liens web.
    """
    pairs: Dict[str, str] = {}
    for message in conv.messages:
        for name, url in FILE_LINK_RE.findall(message.texte or ""):
            if _is_file_link(name, url):
                pairs.setdefault(url, name)
    if not pairs:
        return {"downloaded": 0, "cached": 0, "failed": 0}

    files_dir = Path(files_dir)
    mapping: Dict[str, str] = {}
    stats = {"downloaded": 0, "cached": 0, "failed": 0}
    for url, name in pairs.items():
        existing = _cached_file(files_dir, url)
        if existing is not None:
            mapping[url] = f"{subdir}/{existing.name}"
            stats["cached"] += 1
            continue
        result = loader(url)
        if not result:
            stats["failed"] += 1
            continue
        data, content_type = result
        if not _is_document(data, content_type):
            stats["failed"] += 1
            continue
        filename = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + _file_extension(
            name, content_type, url
        )
        files_dir.mkdir(parents=True, exist_ok=True)
        try:
            (files_dir / filename).write_bytes(data)
        except OSError as exc:
            log.debug("ecriture fichier %s: %s", filename, exc)
            stats["failed"] += 1
            continue
        mapping[url] = f"{subdir}/{filename}"
        stats["downloaded"] += 1

    for message in conv.messages:
        text = message.texte or ""
        for url, relpath in mapping.items():
            if url in text:
                text = text.replace(url, relpath)
        message.texte = text

    log_fields(
        log, 20, "pieces jointes telechargees",
        extra={"platform": getattr(conv, "platform", None), **stats,
               "total": len(pairs)},
    )
    return stats


def download_service_files(conv, service, files_dir: Path) -> Dict[str, int]:
    """Telecharge les pieces jointes via la session du service (cookies)."""
    session = getattr(service, "session", None)
    fetch = getattr(session, "fetch", None)

    def loader(url: str) -> Optional[Tuple[bytes, Optional[str]]]:
        if callable(fetch):
            result = fetch(url)
            if result:
                return result
        return http_loader(url)

    return download_conversation_files(conv, loader, files_dir)


def download_service_media(conv, service, base_dir: Path) -> Dict[str, int]:
    """Images (`images/`) puis pieces jointes (`files/`) sous `base_dir`."""
    base_dir = Path(base_dir)
    images = download_service_images(conv, service, base_dir / IMAGES_SUBDIR)
    files = download_service_files(conv, service, base_dir / FILES_SUBDIR)
    return {
        "images": images,
        "files": files,
    }


def _cached_file(images_dir: Path, url: str) -> Optional[Path]:
    """Fichier deja telecharge pour cette URL (hash inchange), quelle extension."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    if not images_dir.exists():
        return None
    for candidate in images_dir.glob(digest + ".*"):
        return candidate
    return None


def decode_fetch_result(result) -> Optional[Tuple[bytes, Optional[str]]]:
    """Decode le dict renvoye par le JS `fetch` des sessions navigateur."""
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    data = result.get("data")
    if not data:
        return None
    try:
        return base64.b64decode(data), result.get("ct")
    except (ValueError, TypeError):
        return None


# corps JS commun : fetch authentifie -> base64. La variable `url` est injectee.
FETCH_BODY = """
const url = __URL__;
return fetch(url, { credentials: "include" }).then(function (r) {
  if (!r.ok) return { ok: false, status: r.status };
  const ct = r.headers.get("content-type") || "";
  return r.arrayBuffer().then(function (buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return { ok: true, ct: ct, data: btoa(bin) };
  });
}).catch(function (e) {
  return { ok: false, error: String(e) };
});
"""


def build_fetch_body(url: str) -> str:
    return FETCH_BODY.replace("__URL__", json.dumps(url))


def download_service_images(conv, service, images_dir: Path) -> Dict[str, int]:
    """Telecharge les images d'une conversation via la session du service.

    `session.fetch` (cookies authentifies) d'abord, puis repli HTTP simple pour
    les URL publiques/pre-signees (S3, blobs) ou cross-origin (CORS).
    """
    session = getattr(service, "session", None)
    fetch = getattr(session, "fetch", None)

    def loader(url: str) -> Optional[Tuple[bytes, Optional[str]]]:
        if callable(fetch):
            result = fetch(url)
            if result:
                return result
        return http_loader(url)

    return download_conversation_images(conv, loader, images_dir)


# -- URL de medias « seuls » (images / audio / video) -------------------------

#: extensions d'images « seules » (URL directe vers un fichier image)
IMAGE_EXTS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tiff",
    ".avif", ".ico",
)
#: extensions audio « seules »
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac")
#: extensions video « seules »
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")

#: URL http(s) dans un texte (le contexte markdown est classe a part)
URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")


def media_url_kind(url: str) -> Optional[str]:
    """Type du media d'apres l'extension de l'URL ('image'/'audio'/'video') ou None."""
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    for ext in IMAGE_EXTS:
        if path.endswith(ext):
            return "image"
    for ext in AUDIO_EXTS:
        if path.endswith(ext):
            return "audio"
    for ext in VIDEO_EXTS:
        if path.endswith(ext):
            return "video"
    return None


def extract_media_urls(text: str) -> List[Tuple[str, str]]:
    """URL d'un texte : liste de (contexte, url), uniques, dans l'ordre.

    Contexte : 'md-image' (`![alt](url)`), 'md-link' (`[nom](url)`) ou 'bare'
    (URL nue dans la prose). Le filtrage par type de media se fait ensuite
    (`media_url_kind` ; une image markdown est un media meme sans extension).
    """
    if not text:
        return []
    md_images = set(IMAGE_RE.findall(text))
    md_links = {url for _, url in FILE_LINK_RE.findall(text)}
    seen: Dict[str, None] = {}
    out: List[Tuple[str, str]] = []
    for url in URL_RE.findall(text):
        if url in seen:
            continue
        seen[url] = None
        if url in md_images:
            context = "md-image"
        elif url in md_links:
            context = "md-link"
        else:
            context = "bare"
        out.append((context, url))
    return out


def _media_name(url: str) -> str:
    """Nom affichable d'une URL (dernier segment du chemin, sans query)."""
    path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name or url


def _rewrite_media_urls(text: str, mapping: Dict[str, str]) -> str:
    """Reecrit les URL de medias vers leur chemin local.

    Une URL nue vers une image devient un embed `![nom](images/...)`, vers un
    audio/video un lien `[nom](files/...)` ; les URL deja en syntaxe markdown
    (`![alt](url)`, `[nom](url)`) sont remplacees en place.
    """
    if not text or not mapping:
        return text
    md_images = set(IMAGE_RE.findall(text))
    md_links = {url for _, url in FILE_LINK_RE.findall(text)}

    def repl(match: "re.Match[str]") -> str:
        url = match.group(0)
        relpath = mapping.get(url)
        if not relpath:
            return url
        if url in md_images or url in md_links:
            return relpath
        name = _media_name(url)
        if relpath.startswith(IMAGES_SUBDIR + "/"):
            return f"![{name}]({relpath})"
        return f"[{name}]({relpath})"

    return URL_RE.sub(repl, text)


def download_conversation_media_links(
    conv,
    loader: ImageLoader,
    images_dir: Path,
    files_dir: Path,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Telecharge les medias « seuls » pointes par des URL des messages.

    Candidats : images markdown (meme sans extension) et URL (nues ou liens)
    dont l'extension annonce une image/audio/video. Best-effort : le contenu
    est verifie avant ecriture (vraie image / document, pas une page HTML),
    puis l'URL est remplacee dans le texte par le chemin local. `dry_run` :
    telecharge pour verifier mais n'ecrit ni fichier ni texte.
    """
    candidates: Dict[str, str] = {}
    for message in conv.messages:
        for context, url in extract_media_urls(message.texte or ""):
            if context == "md-image":
                candidates.setdefault(url, "image")
                continue
            kind = media_url_kind(url)
            if kind:
                candidates.setdefault(url, kind)
    if not candidates:
        return {"downloaded": 0, "cached": 0, "failed": 0, "candidates": 0}

    images_dir = Path(images_dir)
    files_dir = Path(files_dir)
    mapping: Dict[str, str] = {}
    stats = {"downloaded": 0, "cached": 0, "failed": 0, "candidates": len(candidates)}
    #: (kind, url, chemin local) des medias recuperes — pour le rapport du script
    stats["files"] = []

    for url, kind in candidates.items():
        subdir = IMAGES_SUBDIR if kind == "image" else FILES_SUBDIR
        target_dir = images_dir if kind == "image" else files_dir
        existing = _cached_file(target_dir, url)
        if existing is not None:
            mapping[url] = f"{subdir}/{existing.name}"
            stats["cached"] += 1
            continue
        result = loader(url)
        if not result:
            stats["failed"] += 1
            continue
        data, content_type = result
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
        if kind == "image":
            if not _is_image(data, content_type):
                stats["failed"] += 1
                continue
            filename = digest + _extension(content_type, url)
        else:
            if not _is_document(data, content_type):
                stats["failed"] += 1
                continue
            filename = digest + _file_extension(_media_name(url), content_type, url)
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            try:
                (target_dir / filename).write_bytes(data)
            except OSError as exc:
                log.debug("ecriture media %s: %s", filename, exc)
                stats["failed"] += 1
                continue
        mapping[url] = f"{subdir}/{filename}"
        stats["downloaded"] += 1
        stats["files"].append((kind, url, f"{subdir}/{filename}"))

    if not dry_run:
        for message in conv.messages:
            message.texte = _rewrite_media_urls(message.texte or "", mapping)

    log_fields(
        log, 20, "medias d'URL telecharges",
        extra={"platform": getattr(conv, "platform", None), **stats},
    )
    return stats
