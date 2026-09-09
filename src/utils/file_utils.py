"""Helpers fichiers: noms sur, ecriture atomique, dates, arborescence exports/."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DATE_FMT = "%Y-%m-%d"
STAMP_FMT = "%Y%m%dT%H%M%SZ"


def today_str() -> str:
    return date.today().strftime(DATE_FMT)


def now_stamp() -> str:
    """Horodatage UTC compact pour noms de fichiers (20260909T033000Z)."""
    return datetime.now(timezone.utc).strftime(STAMP_FMT)


def now_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_date_arg(value: str) -> str:
    """Valide/normalise une date fournie en CLI (YYYY-MM-DD)."""
    try:
        parsed = datetime.strptime(value.strip(), DATE_FMT)
    except ValueError as exc:
        raise ValueError(f"date invalide {value!r}, attendu AAAA-MM-JJ") from exc
    return parsed.strftime(DATE_FMT)


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


def export_path(
    output_root: Path, date_str: str, service: str, conversation_id: str
) -> Path:
    """exports/YYYY-MM-DD/<service>/<id>.json"""
    return (
        Path(output_root)
        / date_str
        / slugify(service, 40)
        / f"{safe_id(conversation_id)}.json"
    )


def write_json_atomic(path: Path, payload: Any) -> Path:
    """Ecriture atomique (tmp + os.replace) pour exports utilisables."""
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_json(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def load_state(state_file: Path) -> Dict[str, Any]:
    """Etat d'incrementation: {service: {conversation_id: {...}}}."""
    data = read_json(state_file, default=None)
    if not isinstance(data, dict) or "services" not in data:
        return {"version": 1, "services": {}}
    return data


def save_state(state_file: Path, state: Dict[str, Any]) -> None:
    write_json_atomic(state_file, state)


def record_state(
    state: Dict[str, Any],
    service: str,
    conversation_id: str,
    *,
    title: str,
    message_count: int,
    path: Optional[Path] = None,
    last_message_at: Optional[str] = None,
) -> None:
    """Enregistre/met a jour l'entree d'une conversation dans l'etat."""
    entry = state.setdefault("services", {}).setdefault(service, {})
    entry[conversation_id] = {
        "title": title,
        "message_count": message_count,
        "last_message_at": last_message_at,
        "exported_at": now_iso_z(),
        "path": str(path) if path else None,
    }
