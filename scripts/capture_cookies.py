#!/usr/bin/env python3
"""Capture les cookies d'auth (Grok, Mistral) via un navigateur visible.

Equivalent CLI de `run.py --login grok` / `run.py --login mistral`.

    .venv/bin/python scripts/capture_cookies.py grok
    .venv/bin/python scripts/capture_cookies.py mistral

Ouvre le navigateur, connecte-toi, appuie sur Entree : les cookies du domaine
sont ecrits dans cookies/<service>.json (jamais affiches).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cookies import COOKIE_SERVICES, capture_cookies  # noqa: E402
from src.orchestrator import load_config  # noqa: E402


def _resolved_config() -> dict:
    config = load_config(ROOT / "config.yaml")
    profile_dir = Path(config.get("profile_dir") or (ROOT / "profiles"))
    if not profile_dir.is_absolute():
        profile_dir = ROOT / profile_dir
    config["profile_dir"] = str(profile_dir)
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="+", choices=COOKIE_SERVICES,
                        help="service(s) a capturer")
    args = parser.parse_args()
    config = _resolved_config()
    code = 0
    for service in args.services:
        code |= capture_cookies(service, ROOT / "cookies", config)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
