#!/usr/bin/env python3
"""Assainit les fichiers publies du depot (traces personnelles).

Regles generiques (toujours actives, sans aucun identifiant reel code en dur) :

- UUID reels -> UUID factices reconnaissables (prefixe ``f0000000``) ;
- ids Gemini 16-hex -> ids factices (prefixe ``f00d``) ;
- JWT -> jeton factice ;
- jetons SAS Azure -> valeur ``REDACTED`` ;
- emails de scrape (sentry, calendrier) -> exemples neutres ;
- chemins absolus ``/home/<user>/Documents/code`` -> placeholders.

Les termes propres au deposant (noms, emails personnels, chemins locaux) vivent
dans ``scripts/sanitize.local.json``, **non versionne** :

    {
      "names":  {"Prenom": "Alex", "Nom": "Martin"},
      "emails": {"perso@example.com": "user@example.com"},
      "paths":  {"/home/<user>": "${HOME}"},
      "patterns": ["terme-a-purge"]
    }

    .venv/bin/python scripts/sanitize.py --check   # 0 = propre
    .venv/bin/python scripts/sanitize.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG = ROOT / "scripts" / "sanitize.local.json"

# Namespace fixe : garantit un mapping stable entre deux executions.
NAMESPACE = "aicv-anonymization-v1"

# Termes publics volontairement conserves (pseudonyme du compte GitHub).
PROTECTED = ("Antonio-Faure",)
_SENTINEL = "\x00AICV_HANDLE\x00"

FAKE_UUID_PREFIX = "f0000000-0000-4000-8000-"
FAKE_GEMINI_PREFIX = "f00d"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJmYWtlIjp0cnVlfQ"

# Fichiers a assainir (recursif). Les fichiers de code sans donnee reelle
# (web/, tests hors test_parsers) sont volontairement hors perimetre.
TARGET_GLOBS = (
    "tests/fixtures/**/*",
    "calibration/**/*",
    "tests/test_parsers.py",
    "AGENTS.md",
    "README.md",
    "cookies/README.md",
    ".opencode/**/*.md",
    "scripts/*.py",
    "scripts/etalons.example.json",
)

EXCLUDED_NAMES = {"sanitize.py"}

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)?"
)
SAS_PARAM_RE = re.compile(
    r"(?i)((?:[?&]|\\u0026|&amp;)"
    r"(?:sig|sv|spr|se|skoid|sktid|skt|ske|sks|skv|sr|sp)=)"
    r"(?!REDACTED)[^&\"\\<>)\s]+"
)
SENTRY_RE = re.compile(r"[A-Za-z0-9]{20,}@o\d+\.ingest\.[A-Za-z0-9.]+\.sentry\.io")
#: parametres Sentry embarques dans les pages scrapees (config client)
SENTRY_PARAM_RE = re.compile(r"(?i)(sentry-[a-z_]+=)(?!REDACTED)[^&\"'\s,]+")
CALENDAR_RE = re.compile(r"[A-Za-z0-9._%+-]+@group\.(?:v\.)?calendar\.google\.com")
GEMINI_URL_RE = re.compile(r"(gemini\.google\.com/app/)([0-9a-f]{16})\b")
GEMINI_ID_RE = re.compile(r'("conversation_id"\s*:\s*")([0-9a-f]{16})(")')
#: emails autorises dans le depot (exemples neutres generes par l'assainisseur)
SAFE_EMAIL_RE = re.compile(
    r"@(?:example\.(?:com|work|invalid)|group\.(?:v\.)?calendar\.google\.com)\b",
    re.I,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
#: chemin absolu generique d'un depot local (sans nom d'utilisateur code en dur ;
#: les globs ``/home/*/...`` des permissions d'agents ne sont pas vises)
LOCAL_PATH_RE = re.compile(r"/(?:home|Users)/[^/\"'\s*]+/Documents/code")


def _load_local() -> Dict[str, object]:
    if not LOCAL_CONFIG.exists():
        return {}
    try:
        data = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"sanitize.local.json illisible: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


_LOCAL = _load_local()


def _map(key: str) -> Dict[str, str]:
    value = _LOCAL.get(key) or {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def fake_uuid(value: str) -> str:
    """UUID factice deterministe, prefixe reconnaissable (idempotent)."""
    if value.lower().startswith(FAKE_UUID_PREFIX):
        return value
    digest = hashlib.sha1(f"{NAMESPACE}:{value.lower()}".encode()).hexdigest()
    return f"{FAKE_UUID_PREFIX}{digest[:12]}"


def fake_gemini_id(value: str) -> str:
    """Id Gemini 16-hex factice deterministe (idempotent)."""
    if value.startswith(FAKE_GEMINI_PREFIX):
        return value
    digest = hashlib.sha1(f"{NAMESPACE}:{value}".encode()).hexdigest()
    return f"{FAKE_GEMINI_PREFIX}{digest[:12]}"


def _mask(text: str) -> str:
    """Protege les termes publics (pseudonyme) des remplacements."""
    for term in PROTECTED:
        text = text.replace(term, _SENTINEL)
    return text


def _unmask(text: str) -> str:
    return text.replace(_SENTINEL, PROTECTED[0])


def sanitize_text(text: str) -> str:
    """Applique toutes les regles d'assainissement a un texte."""
    text = _mask(text)
    text = JWT_RE.sub(lambda m: m.group(0) if m.group(0) == FAKE_JWT else FAKE_JWT, text)
    text = SAS_PARAM_RE.sub(r"\1REDACTED", text)
    for real, fake in _map("emails").items():
        text = text.replace(real, fake)
    text = CALENDAR_RE.sub("calendar@example.com", text)
    text = SENTRY_RE.sub("key@example.invalid", text)
    text = SENTRY_PARAM_RE.sub(r"\1REDACTED", text)
    text = GEMINI_URL_RE.sub(lambda m: f"{m.group(1)}{fake_gemini_id(m.group(2))}", text)
    text = GEMINI_ID_RE.sub(
        lambda m: f"{m.group(1)}{fake_gemini_id(m.group(2))}{m.group(3)}", text
    )
    for real, fake in _map("paths").items():
        text = text.replace(real, fake)
    for real, fake in _map("names").items():
        text = text.replace(real, fake)
    return _unmask(UUID_RE.sub(lambda m: fake_uuid(m.group(0)), text))


def sanitize_bytes(data: bytes) -> bytes:
    """Version bytes, pour les blobs git (filter-repo) et les fichiers."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return sanitize_text(text).encode("utf-8")


def _findings() -> Tuple[Tuple[str, re.Pattern[str]], ...]:
    """Motifs generiques + motifs locaux (config non versionnee)."""
    base: Tuple[Tuple[str, re.Pattern[str]], ...] = (
        ("chemin", LOCAL_PATH_RE),
        ("sas", SAS_PARAM_RE),
        ("sentry", SENTRY_PARAM_RE),
    )
    local: List[Tuple[str, re.Pattern[str]]] = []
    for term in list(_map("names")) + [str(p) for p in _LOCAL.get("patterns") or []]:
        local.append(("terme-local", re.compile(re.escape(term), re.I)))
    for email in _map("emails"):
        local.append(("email-local", re.compile(re.escape(email), re.I)))
    return base + tuple(local)


def scan_text(text: str, label: str, check_uuids: bool = True) -> List[str]:
    """Retourne les problemes restants (liste vide si propre)."""
    text = _mask(text)
    problems: List[str] = []
    for name, pattern in _findings():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{label}:{line}: {name}")
    for match in EMAIL_RE.finditer(text):
        if not SAFE_EMAIL_RE.search(match.group(0)):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{label}:{line}: email")
    if check_uuids:
        for match in UUID_RE.finditer(text):
            value = match.group(0)
            if not value.lower().startswith(FAKE_UUID_PREFIX):
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{label}:{line}: uuid")
    for match in JWT_RE.finditer(text):
        if match.group(0) != FAKE_JWT:
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{label}:{line}: jwt")
    return problems


def check_text(text: str, label: str) -> List[str]:
    """Controle complet d'un fichier assaini (UUID compris)."""
    return scan_text(text, label, check_uuids=True)


def _targets() -> List[Path]:
    paths: List[Path] = []
    for pattern in TARGET_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file() or path.name in EXCLUDED_NAMES:
                continue
            if any(part in {".git", "__pycache__", "node_modules"} for part in path.parts):
                continue
            paths.append(path)
    return paths


def run(apply: bool) -> int:
    problems: List[str] = []
    changed = 0
    for path in _targets():
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        cleaned = sanitize_text(text)
        if cleaned != text:
            if apply:
                path.write_text(cleaned, encoding="utf-8")
                changed += 1
                print(f"assaini: {rel}")
                text = cleaned
            else:
                problems.append(f"{rel}: a assainir")
        problems.extend(check_text(text, rel))
    if problems:
        print(f"\n{len(problems)} probleme(s) restant(s):", file=sys.stderr)
        for line in problems[:40]:
            print(f"  {line}", file=sys.stderr)
        return 1
    if not apply:
        print("propre")
    else:
        print(f"{changed} fichier(s) assaini(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="verifie sans modifier")
    group.add_argument("--apply", action="store_true", help="reecrit les fichiers")
    args = parser.parse_args()
    return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
