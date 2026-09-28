"""Fingerprints navigateur stables et isoles par profil.

Chaque chatbot a son profil persistant (`profiles/<service>/`) et un
fingerprint propre (User-Agent, taille de fenetre, langue). Le fingerprint est
derive du nom du service et **persiste** dans le profil : stable entre les
executions (indispensable pour que les cookies anti-bot lies a l'UA, comme
cf_clearance, restent valides) et different d'un chatbot a l'autre.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .utils.file_utils import ensure_dir, write_json_atomic

#: User-Agents Linux/Chrome plausibles (le binaire Chrome est le meme partout)
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

WINDOW_SIZES: List[Tuple[int, int]] = [
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1366, 768),
    (1920, 1080),
    (1280, 800),
]

LANGS: List[str] = ["en-US", "en-GB", "fr-FR"]

HARDWARE_CONCURRENCY = [4, 6, 8, 12, 16]
DEVICE_MEMORY = [4, 8, 16]

FINGERPRINT_FILE = "fingerprint.json"


def _seed(service: str) -> int:
    return int(hashlib.sha256(service.encode("utf-8")).hexdigest()[:12], 16)


def generate_fingerprint(service: str) -> Dict[str, Any]:
    """Fingerprint deterministe pour un service (meme valeur a chaque appel)."""
    rng = random.Random(_seed(service))
    width, height = rng.choice(WINDOW_SIZES)
    lang = rng.choice(LANGS)
    return {
        "service": service,
        "user_agent": rng.choice(USER_AGENTS),
        "window_width": width,
        "window_height": height,
        "lang": lang,
        "languages": [lang, lang.split("-")[0]],
        "hardware_concurrency": rng.choice(HARDWARE_CONCURRENCY),
        "device_memory": rng.choice(DEVICE_MEMORY),
    }


def get_fingerprint(service: str, profile_dir: Path | str) -> Dict[str, Any]:
    """Charge le fingerprint du profil, ou le cree puis le persiste."""
    profile_path = Path(profile_dir)
    path = profile_path / FINGERPRINT_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("user_agent"):
                return data
        except json.JSONDecodeError:
            pass
    fingerprint = generate_fingerprint(service)
    ensure_dir(profile_path)
    write_json_atomic(path, fingerprint)
    return fingerprint
