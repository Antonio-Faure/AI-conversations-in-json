"""Payload API brut : `exports/<platform>/raw/<slug>.json`.

Les plateformes scrapees par API (ChatGPT, Claude, Gemini, Grok, Mistral,
Perplexity) ne produisent plus de HTML : le payload de l'API est conserve tel
quel, comme l'est le HTML brut des plateformes DOM, pour permettre un
re-mapping sans re-telechargement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .file_utils import platform_dir, read_json, write_json_atomic

#: nom du sous-dossier des payloads bruts dans exports/<platform>/
RAW_DIR_NAME = "raw"


def raw_payload_dir(output_root: Path, platform: str) -> Path:
    """exports/<platform>/raw/"""
    return platform_dir(output_root, platform) / RAW_DIR_NAME


def raw_payload_path(output_root: Path, platform: str, slug: str) -> Path:
    """Chemin du payload brut (`slug` = stem du JSON de la conversation)."""
    return raw_payload_dir(output_root, platform) / f"{slug}.json"


def save_raw_payload(output_root: Path, platform: str, slug: str, payload: Any) -> Path:
    """Ecrit le payload API brut en JSON indente (ecriture atomique)."""
    return write_json_atomic(raw_payload_path(output_root, platform, slug), payload)


def load_raw_payload(
    output_root: Path, platform: str, slug: str
) -> Optional[Any]:
    """Charge un payload brut ; None s'il est absent ou illisible."""
    return read_json(raw_payload_path(output_root, platform, slug), default=None)
