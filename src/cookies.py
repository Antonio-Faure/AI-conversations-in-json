"""Capture de cookies d'authentification via un navigateur visible.

Deux modes selon le service (champ `capture_via` dans cookies/<service>.json) :

  - "browser" (defaut) : navigateur du projet (Playwright/Botasaurus, stealth,
    UA, anti-Cloudflare).
  - "firefox"  : lance le Firefox systeme NON automatise, puis lit
    cookies.sqlite du profil dedie (indispensable quand Cloudflare bloque le
    Chromium pilote, ex: Grok).

La connexion est detectee automatiquement via `auth_cookies` (noms des cookies
de session) : le navigateur se ferme des qu'ils apparaissent. On peut aussi
appuyer sur Entree pour forcer. Les valeurs des cookies ne sont jamais
affichees ; le JSON n'est pas versionne.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .utils.file_utils import now_iso_z, write_json_atomic

#: plateformes authentifiees par injection de cookies (pas encore scrapees)
COOKIE_SERVICES: tuple[str, ...] = ("grok", "mistral")

DEFAULT_TIMEOUT_S = 900


def cookies_path(cookies_dir: Path | str, service: str) -> Path:
    return Path(cookies_dir) / f"{service}.json"


def _domain_matches(cookie_domain: str, wanted: str) -> bool:
    cookie_domain = (cookie_domain or "").lstrip(".").lower()
    wanted = (wanted or "").lstrip(".").lower()
    if not wanted:
        return True
    return cookie_domain == wanted or cookie_domain.endswith("." + wanted)


def _wait_for_login(
    check: Callable[[], bool],
    proc: Optional[subprocess.Popen] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """Attend la connexion (cookies presents) ou Entree. Retourne la raison."""
    stop = threading.Event()

    def _read_enter() -> None:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        stop.set()

    threading.Thread(target=_read_enter, daemon=True).start()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if stop.is_set():
            return "enter"
        if proc is not None and proc.poll() is not None:
            return "closed"
        try:
            if check():
                return "auto"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return "timeout"


# -- mode "browser" (moteur du projet) ----------------------------------------


def _login_session(service: str, config: Dict[str, Any]):
    from .orchestrator import default_browser_factory, resolve_engine

    engine = resolve_engine(service, config)
    if engine == "auto":
        engine = "playwright"
    session_config = dict(config, _engine=engine, headless=False)
    profile_dir = Path(config.get("profile_dir") or "profiles")
    return default_browser_factory(profile_dir, service, session_config)


def _capture_via_browser(service: str, app_url: str, wanted: str,
                         config: Dict[str, Any], auth_names: List[str]) -> Dict[str, str]:
    session = _login_session(service, config)

    def logged_in() -> bool:
        cookies = session.cookies()
        if auth_names:
            return any(c.get("name") in auth_names for c in cookies)
        return any(_domain_matches(c.get("domain", ""), wanted) for c in cookies)

    captured: Dict[str, str] = {}
    try:
        session.start()
        session.goto(app_url)
        print(f"[{service}] Navigateur ouvert sur {app_url}. Connecte-toi "
              f"(fermeture auto une fois connecte, ou Entree).")
        reason = _wait_for_login(logged_in)
        print(f"[{service}] connexion detectee ({reason})")
        for cookie in session.cookies():
            name = cookie.get("name")
            if name and _domain_matches(cookie.get("domain", ""), wanted):
                captured[str(name)] = str(cookie.get("value", ""))
    finally:
        try:
            session.goto("about:blank")
        except Exception:  # noqa: BLE001
            pass
        session.close()
    return captured


# -- mode "firefox" (navigateur reel, non automatise) --------------------------


def _find_firefox() -> Optional[str]:
    for name in ("firefox", "firefox-esr"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _read_firefox_cookies(profile_dir: Path, wanted: str) -> Dict[str, str]:
    db = profile_dir / "cookies.sqlite"
    if not db.exists():
        return {}
    out: Dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="aicv_ff_") as tmp:
        target = Path(tmp) / "cookies.sqlite"
        # copie DB + WAL/SHM pour une lecture coherente pendant que Firefox tourne
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(target) + suffix))
        try:
            conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT name, value, host FROM moz_cookies"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return {}
        for name, value, host in rows:
            if _domain_matches(host, wanted):
                out[str(name)] = str(value)
    return out


def _capture_via_firefox(service: str, app_url: str, wanted: str,
                         profile_dir: Path, auth_names: List[str]) -> Dict[str, str]:
    binary = _find_firefox()
    if not binary:
        print(f"[{service}] Firefox introuvable (apt install firefox)")
        return {}
    profile_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [binary, "--no-remote", "--profile", str(profile_dir), app_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def logged_in() -> bool:
        cookies = _read_firefox_cookies(profile_dir, wanted)
        if not auth_names:
            return bool(cookies)
        return any(name in cookies for name in auth_names)

    print(f"[{service}] Firefox (reel) ouvert sur {app_url}. Connecte-toi "
          f"(fermeture auto une fois connecte, ou Entree).")
    try:
        reason = _wait_for_login(logged_in, proc=proc)
        print(f"[{service}] connexion detectee ({reason})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)
    return _read_firefox_cookies(profile_dir, wanted)


# -- point d'entree -----------------------------------------------------------


def capture_cookies(service: str, cookies_dir: Path | str, config: Dict[str, Any]) -> int:
    """Ouvre un navigateur visible, capture les cookies, ecrit le JSON."""
    path = cookies_path(cookies_dir, service)
    if not path.exists():
        print(f"[{service}] fichier absent: {path}")
        return 1
    try:
        file_config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[{service}] JSON invalide: {exc}")
        return 1

    app_url = str(file_config.get("app_url") or "")
    if not app_url:
        print(f"[{service}] 'app_url' manquant dans {path}")
        return 1
    wanted = str(file_config.get("domain") or "")
    mode = str(file_config.get("capture_via") or "browser").lower()
    auth_names = [str(n) for n in (file_config.get("auth_cookies") or [])]

    if mode == "firefox":
        profile_dir = Path(config.get("profile_dir") or "profiles") / f"{service}-firefox"
        captured = _capture_via_firefox(service, app_url, wanted, profile_dir, auth_names)
    else:
        captured = _capture_via_browser(service, app_url, wanted, config, auth_names)

    if not captured:
        print(f"[{service}] aucun cookie capture (domaine {wanted!r}) -> rien ecrit")
        return 1
    updated = dict(file_config)
    updated["cookies"] = captured
    updated["captured_at"] = now_iso_z()
    write_json_atomic(path, updated)
    print(f"[{service}] {len(captured)} cookie(s) enregistres dans {path}")
    print(f"[{service}] teste: scripts/check_cookies.py {service}")
    return 0
