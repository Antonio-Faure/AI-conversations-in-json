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
        text = message.texte
        for url, relpath in mapping.items():
            if url in text:
                text = text.replace(url, relpath)
        message.texte = text

    log_fields(
        log, 20, "images telechargees",
        extra={"platform": getattr(conv, "platform", None), **stats,
               "total": len(urls)},
    )
    return stats


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

    Utilise `session.fetch` (cookies authentifies) si disponible, sinon un
    telechargement HTTP simple (URL publiques).
    """
    session = getattr(service, "session", None)
    fetch = getattr(session, "fetch", None)
    loader: ImageLoader = fetch if callable(fetch) else http_loader
    return download_conversation_images(conv, loader, images_dir)
