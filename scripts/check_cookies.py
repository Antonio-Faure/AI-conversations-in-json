#!/usr/bin/env python3
"""Verifie rapidement l'authentification par cookies (Grok, Mistral).

Base pour les futurs scrapers (phases 5 et 6) : lit cookies/<service>.json,
injecte les cookies dans un curl et indique si la session est valide.

    .venv/bin/python scripts/check_cookies.py grok
    .venv/bin/python scripts/check_cookies.py mistral
    .venv/bin/python scripts/check_cookies.py            # tous

Les valeurs des cookies ne sont jamais affichees. Le fichier cookies/*.json
n'est pas versionne.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
COOKIES_DIR = ROOT / "cookies"
SERVICES = ("grok", "mistral")
STATUS_MARKER = "__HTTP_STATUS__"


def load_config(service: str) -> Optional[Dict[str, Any]]:
    path = COOKIES_DIR / f"{service}.json"
    if not path.exists():
        print(f"[{service}] fichier absent : {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[{service}] JSON invalide dans {path}: {exc}")
        return None
    if not isinstance(data, dict):
        print(f"[{service}] format invalide (attendu un objet JSON)")
        return None
    return data


def cookie_pairs(config: Dict[str, Any]) -> List[tuple]:
    """Cookies non vides, dans l'ordre du fichier."""
    cookies = config.get("cookies") or {}
    if not isinstance(cookies, dict):
        return []
    return [(str(k), str(v)) for k, v in cookies.items() if str(v or "").strip()]


def write_cookie_file(domain: str, pairs: List[tuple]) -> str:
    """Ecrit un fichier cookie Netscape (0600) pour `curl -b`."""
    host = domain.lstrip(".") or "localhost"
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in pairs:
        lines.append(f"{host}\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
    fd, path = tempfile.mkstemp(prefix="aicv_cookies_", suffix=".txt")
    os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    os.close(fd)
    os.chmod(path, 0o600)
    return path


def run_curl(url: str, cookie_file: str, headers: Dict[str, str], timeout_s: int = 25):
    cmd = [
        "curl", "-sS", "-L", "--max-time", str(timeout_s),
        "-b", cookie_file, "-w", f"\n{STATUS_MARKER}%{{http_code}}", url,
    ]
    for key, value in (headers or {}).items():
        cmd += ["-H", f"{key}: {value}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 10)
    except FileNotFoundError:
        return None, "curl introuvable"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    output = proc.stdout or ""
    status = None
    if STATUS_MARKER in output:
        body, _, tail = output.rpartition(STATUS_MARKER)
        try:
            status = int(tail.strip())
        except ValueError:
            status = None
    else:
        body = output
    if proc.returncode != 0 and status is None:
        return None, (proc.stderr or "echec curl").strip()
    return status, body


def check(service: str) -> bool:
    config = load_config(service)
    if config is None:
        return False

    pairs = cookie_pairs(config)
    if not pairs:
        print(f"[{service}] aucun cookie renseigne -> colle tes cookies dans "
              f"{COOKIES_DIR / (service + '.json')}")
        return False

    domain = str(config.get("domain") or "")
    url = str(config.get("check_url") or config.get("app_url") or "")
    if not url:
        print(f"[{service}] 'check_url' manquant dans le fichier de cookies")
        return False

    # les cookies sont envoyes au domaine de l'URL testee (pas du champ domain)
    host = urlparse(url).hostname or domain
    cookie_file = write_cookie_file(host, pairs)
    try:
        status, body = run_curl(url, cookie_file, config.get("headers") or {})
    finally:
        try:
            os.remove(cookie_file)
        except OSError:
            pass

    if status is None:
        print(f"[{service}] echec de la requete: {body}")
        return False

    body_l0 = (body or "").lower()
    if status in (403, 503) and any(
        m in body_l0 for m in ("just a moment", "cloudflare", "attention required")
    ):
        print(f"[{service}] verif HTTP impossible : Cloudflare bloque curl (HTTP {status}). "
              f"Les cookies restent utilisables via le navigateur du projet.")
        return False

    markers = [str(m).lower() for m in (config.get("authenticated_markers") or [])]
    success_status = config.get("success_status") or [200]
    body_l = (body or "").lower()
    marker_ok = any(m in body_l for m in markers) if markers else False
    status_ok = status in success_status

    authenticated = marker_ok or (status_ok and not markers)
    state = "AUTHENTIFIE" if authenticated else "NON authentifie"
    print(f"[{service}] {state} (HTTP {status}, {len(pairs)} cookie(s) envoyes)")
    if not authenticated:
        print(f"[{service}] verifie/rafraichis les cookies, ou ajuste 'check_url'/"
              f"'authenticated_markers' dans le fichier.")
    return authenticated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="*", choices=SERVICES,
                        help="service(s) a tester (defaut: tous)")
    args = parser.parse_args()
    targets = args.services or list(SERVICES)
    results = {service: check(service) for service in targets}
    ok = all(results.values())
    print("OK" if ok else "ECHEC", "->", ", ".join(
        f"{s}:{'ok' if r else 'ko'}" for s, r in results.items()
    ))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
