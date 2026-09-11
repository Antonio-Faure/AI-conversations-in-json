"""Helpers fichiers: noms surs, ecriture atomique, arborescence exports/."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STAMP_FMT = "%Y%m%dT%H%M%SZ"

#: nom reserve du repertoire de sortie d'une plateforme
CONVERSATION_LIST_NAME = "conversation_list.json"


def now_stamp() -> str:
    """Horodatage UTC compact pour noms de fichiers (20260909T033000Z)."""
    return datetime.now(timezone.utc).strftime(STAMP_FMT)


def now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dir(path: Path) -> Path:
    Path(path).mkdir(parents=True, exist_ok=True)
    return Path(path)


_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify(text: str, max_length: int = 80) -> str:
    """Titre -> identifiant sur pour nom de fichier (sans dependance externe)."""
    if not text:
        return "untitled"
    normalized = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    slug = _SLUG_RE.sub("-", normalized.lower()).strip("-.")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:max_length].rstrip("-.") or "untitled").lower()


def safe_id(raw: str) -> str:
    """Nettoie un id de conversation venant d'une URL (charset tres restrictif)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(raw).strip())
    return cleaned or "unknown"


def unique_filename(
    title: Optional[str],
    conversation_id: str,
    used: Dict[str, str],
    max_length: int = 80,
) -> str:
    """Nom de fichier (sans extension) derive du titre, unique par conversation.

    `used` associe un nom de base a l'id de conversation qui le possede : une
    collision de titre est desambiguisee par un suffixe d'id.
    """
    base = slugify(title or "", max_length)
    if base in ("untitled", ""):
        base = "conversation"
    owner = used.get(base)
    if owner is None or owner == conversation_id:
        used[base] = conversation_id
        return base
    suffix = safe_id(conversation_id)[:8]
    candidate = f"{base}-{suffix}"
    counter = 2
    while candidate in used and used[candidate] != conversation_id:
        candidate = f"{base}-{suffix}-{counter}"
        counter += 1
    used[candidate] = conversation_id
    return candidate


# -- arborescence de sortie : exports/<platform>/ --------------------------------


def platform_dir(output_root: Path, platform: str) -> Path:
    """exports/<platform>/"""
    return Path(output_root) / slugify(platform, 40)


def conversation_list_path(output_root: Path, platform: str) -> Path:
    return platform_dir(output_root, platform) / CONVERSATION_LIST_NAME


def conversation_paths(output_root: Path, platform: str, stem: str) -> tuple[Path, Path]:
    """Retourne (chemin JSON, chemin HTML) pour un nom de conversation."""
    directory = platform_dir(output_root, platform)
    return directory / f"{stem}.json", directory / f"{stem}.html"


# -- ecriture atomique -----------------------------------------------------------


def write_json_atomic(path: Path, payload: Any) -> Path:
    """Ecriture atomique (tmp + os.replace) pour exports utilisables."""
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    return write_text_atomic(path, text)


def write_text_atomic(path: Path, text: str) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


#: champs volatils (bookkeeping) ignores dans la comparaison de contenu
_VOLATILE_KEYS = ("exported_at",)


def _content_only(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if k not in _VOLATILE_KEYS}


def json_content_equal(existing: Dict[str, Any], new: Dict[str, Any]) -> bool:
    """True si le contenu metier est identique (hors champs volatils)."""
    if not isinstance(existing, dict) or not isinstance(new, dict):
        return False
    return _content_only(existing) == _content_only(new)


def merge_conversation_json(
    existing: Any, new: Dict[str, Any]
) -> tuple[Dict[str, Any], str, int]:
    """Fusionne une conversation exportee avec le fichier existant.

    Retourne (payload, status, appended) ou status vaut :
      - "written"   : pas de fichier existant
      - "unchanged" : contenu identique -> ne pas ecrire
      - "patched"   : les anciens messages sont un prefixe -> ajout des nouveaux
      - "rewritten" : contenu divergent -> reecriture complete
    """
    if not isinstance(existing, dict):
        return new, "written", len(new.get("messages") or [])
    if json_content_equal(existing, new):
        return existing, "unchanged", 0
    old_messages = existing.get("messages") or []
    new_messages = new.get("messages") or []
    if (
        old_messages
        and len(new_messages) > len(old_messages)
        and new_messages[: len(old_messages)] == old_messages
    ):
        merged = dict(new)
        merged["messages"] = old_messages + new_messages[len(old_messages):]
        return merged, "patched", len(new_messages) - len(old_messages)
    return new, "rewritten", len(new_messages)


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


# -- conversation_list.json ------------------------------------------------------


def load_conversation_list(path: Path) -> Dict[str, Dict[str, Any]]:
    """Charge conversation_list.json en index {conversation_id: entree}."""
    data = read_json(path, default=None)
    conversations = []
    if isinstance(data, dict):
        conversations = data.get("conversations") or []
    elif isinstance(data, list):
        conversations = data
    index: Dict[str, Dict[str, Any]] = {}
    for entry in conversations:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("conversation_id")
        if cid:
            index[str(cid)] = entry
    return index


def save_conversation_list(
    path: Path, platform: str, index: Dict[str, Dict[str, Any]]
) -> Path:
    """Ecrit conversation_list.json (entries triees par date decroissante)."""

    def sort_key(entry: Dict[str, Any]):
        return (entry.get("last_message_at") or "", entry.get("title") or "")

    entries = sorted(index.values(), key=sort_key, reverse=True)
    payload = {
        "platform": platform,
        "updated_at": now_iso_z(),
        "count": len(entries),
        "conversations": entries,
    }
    return write_json_atomic(path, payload)
