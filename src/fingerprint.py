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
import os
import random
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .utils.file_utils import ensure_dir, write_json_atomic

#: User-Agent construit sur la version majeure REELLE du navigateur local :
#: un UA qui ne colle pas au binaire (ou aux Client Hints) est detecte par
#: Cloudflare et provoque une boucle de captcha.
USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

#: Repli si aucun binaire local n'est detectable (CI, machine sans navigateur).
USER_AGENTS: List[str] = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

_CHROME_MAJOR: Optional[str] = None
_CHROME_MAJOR_RESOLVED = False


def installed_chrome_major() -> Optional[str]:
    """Version majeure du Chrome/Chromium local (cachee, sans lancer le moteur)."""
    global _CHROME_MAJOR, _CHROME_MAJOR_RESOLVED
    if _CHROME_MAJOR_RESOLVED:
        return _CHROME_MAJOR
    _CHROME_MAJOR_RESOLVED = True
    candidates: List[Path] = []
    env_path = os.environ.get("AICV_CHROME_PATH")
    if env_path:
        candidates.append(Path(env_path))
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    root = Path(pw_path) if pw_path else Path.home() / ".cache" / "ms-playwright"
    try:
        candidates.extend(sorted(root.glob("chromium-*/chrome-linux*/chrome")))
    except OSError:
        pass
    for binary in reversed(candidates):
        try:
            out = subprocess.run(
                [str(binary), "--version"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"(\d+)\.", out)
        if match:
            _CHROME_MAJOR = match.group(1)
            return _CHROME_MAJOR
    return None

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
    major = installed_chrome_major()
    user_agent = (
        USER_AGENT_TEMPLATE.format(major=major)
        if major
        else rng.choice(USER_AGENTS)
    )
    return {
        "service": service,
        "user_agent": user_agent,
        "window_width": width,
        "window_height": height,
        "lang": lang,
        "languages": [lang, lang.split("-")[0]],
        "hardware_concurrency": rng.choice(HARDWARE_CONCURRENCY),
        "device_memory": rng.choice(DEVICE_MEMORY),
    }


def get_fingerprint(service: str, profile_dir: Path | str) -> Dict[str, Any]:
    """Charge le fingerprint du profil, ou le cree puis le persiste.

    Repare automatiquement un UA obsolete (majeure differente du navigateur
    installe) : c'est indispensable pour cf_clearance, lie a l'UA.
    """
    profile_path = Path(profile_dir)
    path = profile_path / FINGERPRINT_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("user_agent"):
                major = installed_chrome_major()
                if major and f"Chrome/{major}." not in str(data["user_agent"]):
                    data["user_agent"] = USER_AGENT_TEMPLATE.format(major=major)
                    write_json_atomic(path, data)
                return data
        except json.JSONDecodeError:
            pass
    fingerprint = generate_fingerprint(service)
    ensure_dir(profile_path)
    write_json_atomic(path, fingerprint)
    return fingerprint
