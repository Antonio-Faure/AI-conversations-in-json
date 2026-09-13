"""Moteur Botasaurus (botasaurus_driver, CDP pur) : meme facade que BrowserSession.

Meme contrat que src/browser.py (goto, html, evaluate, wait_for_any, scroll,
looks_blocked, try_solve_cloudflare, ...) mais pilote un Chrome patche via
botasaurus_driver, capable de contourner Cloudflare/Turnstile :

    session = BotasaurusSession(profile_dir=..., headless=True)
    with session:
        session.goto("https://claude.ai/chats", bypass_cloudflare=True)

Contrairement a Playwright, pas d'objet `page` : tout passe par la facade
(les services ne doivent pas accéder à session.page).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .browser import ScrollResult
from .selectors import (
    Sel,
    js_click_body,
    js_presence_body,
    js_scroll_body,
    playwright_js_to_iife,
    translate_selector,
)
from .utils.logging import get_logger, log_fields

log = get_logger("browser_bota")

DEFAULT_WINDOW_SIZE = (1440, 900)


def find_chrome_binary() -> Optional[str]:
    """Cherche un binaire Chrome utilisable (surcharge via AICV_CHROME_PATH).

    botasaurus telecharge le sien si rien n'est trouve ; ici on reutilise en
    priorite le Chromium installe par Playwright pour eviter un second download.
    """
    env = os.environ.get("AICV_CHROME_PATH")
    if env and Path(env).is_file():
        return env
    home = Path.home() / ".cache" / "ms-playwright"
    patterns = (
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux64/headless_shell",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    )
    for pattern in patterns:
        for p in sorted(home.glob(pattern)):
            if p.is_file():
                return str(p)
    return None


class BotasaurusSession:
    """Facade navigateur basee sur botasaurus_driver (anti-Cloudflare)."""

    def __init__(
        self,
        profile_dir: Optional[Path | str] = None,
        headless: bool = False,
        timeout_ms: int = 45000,
        chrome_executable_path: Optional[str] = None,
        enable_xvfb: bool = False,
        service: Optional[str] = None,
        fingerprint: Optional[Dict[str, Any]] = None,
    ):
        self.profile_dir = Path(profile_dir or "profiles")
        self.headless = headless
        self.timeout_ms = int(timeout_ms)
        self.chrome_path = chrome_executable_path or None
        self.enable_xvfb = bool(enable_xvfb)
        self.service = service
        self.fingerprint = fingerprint or {}
        self._driver = None

    # -- cycle de vie ---------------------------------------------------------

    def start(self) -> "BotasaurusSession":
        if self._driver is not None:
            return self
        from botasaurus_driver import Driver

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        chrome = self.chrome_path or find_chrome_binary()
        fp = self.fingerprint or {}
        window_size = (
            (int(fp["window_width"]), int(fp["window_height"]))
            if fp.get("window_width")
            else DEFAULT_WINDOW_SIZE
        )
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "profile": str(self.profile_dir),
            "window_size": window_size,
        }
        if fp.get("user_agent"):
            kwargs["user_agent"] = fp["user_agent"]
        if fp.get("lang"):
            kwargs["lang"] = fp["lang"]
        if chrome:
            kwargs["chrome_executable_path"] = chrome
        else:
            log_fields(
                log, 30,
                "aucun chrome detecte : botasaurus utilisera son propre binaire",
            )
        # Xvfb : Chrome "headful" dans un affichage virtuel -> indetectable
        # comme headless (Cloudflare blockait le headless pur sur perplexity).
        if self.enable_xvfb and self.headless:
            kwargs["headless"] = False
            kwargs["enable_xvfb_virtual_display"] = True
        try:
            self._driver = Driver(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if kwargs.get("enable_xvfb_virtual_display"):
                log_fields(
                    log, 30,
                    f"Xvfb indisponible ({exc}) -> nouvelle tentative en headless pur",
                )
                kwargs["headless"] = True
                kwargs.pop("enable_xvfb_virtual_display", None)
                self._driver = Driver(**kwargs)
            else:
                raise
        log_fields(
            log, 20, "botasaurus started",
            extra={"profile": str(self.profile_dir), "headless": self.headless,
                   "xvfb": self.enable_xvfb and self.headless,
                   "chrome": chrome or "bundled"},
        )
        return self

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:  # noqa: BLE001
                pass
            # ferme reellement le navigateur si l'API l'expose
            try:
                quit_fn = getattr(self._driver, "quit", None)
                if callable(quit_fn):
                    quit_fn()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None

    def __enter__(self) -> "BotasaurusSession":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- helpers internes ------------------------------------------------------

    @property
    def driver(self):
        self.start()
        assert self._driver is not None
        return self._driver

    def _timeout_s(self) -> int:
        return max(30, self.timeout_ms // 1000)

    def _run_js(self, body: str, args: Optional[list] = None) -> Any:
        try:
            return self.driver.run_js(body, args=args)
        except Exception as exc:  # noqa: BLE001
            log.debug("run_js failed: %s", exc)
            return None

    def _present(self, sel: Sel, wait_s: float = 0.0) -> bool:
        d = self.driver
        body = js_presence_body(sel)
        if body is None:
            try:
                return bool(d.is_element_present(sel.css, wait=wait_s or None))
            except Exception:  # noqa: BLE001
                return False
        deadline = time.monotonic() + max(0.0, wait_s)
        while True:
            if self._run_js(body):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.3)

    # -- navigation ------------------------------------------------------------

    def goto(self, url: str, wait_until: str = "domcontentloaded",
             bypass_cloudflare: bool = True) -> Any:
        """Ouvre `url` ; bypass_cloudflare laisse botasaurus gerer le challenge."""
        d = self.driver
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                return d.get(
                    url,
                    bypass_cloudflare=bypass_cloudflare,
                    timeout=self._timeout_s(),
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log_fields(
                    log, 30, "navigation retry",
                    extra={"url": url, "attempt": attempt + 1, "error": str(exc)},
                )
                time.sleep(1 + attempt)
        raise RuntimeError(f"navigation impossible vers {url}: {last_error}")

    def reload(self) -> None:
        try:
            self.driver.reload()
        except Exception as exc:  # noqa: BLE001
            log.debug("reload failed: %s", exc)

    def wait_ms(self, ms: int) -> None:
        time.sleep(max(0, ms) / 1000)

    def wait_for_any(
        self,
        selectors: Sequence[str],
        timeout_ms: Optional[int] = None,
    ) -> Optional[str]:
        """Attend le premier selecteur visible parmi `selectors`. Retourne celui-ci."""
        per = max(1500, (timeout_ms or self.timeout_ms) // max(1, len(selectors)))
        for raw in selectors:
            sel = translate_selector(raw)
            if self._present(sel, wait_s=per / 1000):
                return raw
        return None

    # -- lecture DOM -----------------------------------------------------------

    def html(self) -> str:
        return self.driver.page_html or ""

    def url(self) -> str:
        return self.driver.current_url or ""

    def cookies(self) -> List[Dict[str, Any]]:
        """Cookies du navigateur (pour la capture d'auth)."""
        try:
            return list(self.driver.get_cookies() or [])
        except Exception:  # noqa: BLE001
            return []

    def title(self) -> str:
        try:
            return self.driver.title or ""
        except Exception:  # noqa: BLE001
            return ""

    def fetch(self, url: str):
        """Telecharge une URL (image signee) avec les cookies du navigateur."""
        from .utils.images import build_fetch_body, decode_fetch_result

        return decode_fetch_result(self._run_js(build_fetch_body(url)))

    def evaluate(self, script: str, arg: Any = None) -> Any:
        args = [arg] if arg is not None else None
        return self._run_js(playwright_js_to_iife(script), args=args)

    def eval_body(self, body: str) -> Any:
        """Execute un corps JS `return ...` (facade commune aux 2 moteurs)."""
        return self._run_js(body)

    def is_element_present(self, selector: str) -> bool:
        """Presence immediate d'un element (facade commune aux deux moteurs)."""
        return self._present(translate_selector(selector), wait_s=0.0)

    def click_if_present(self, selector: str, timeout_ms: int = 2500) -> bool:
        sel = translate_selector(selector)
        body = js_click_body(sel)
        if body is None:
            try:
                self.driver.click(sel.css, wait=max(1, timeout_ms // 1000))
                return True
            except Exception:  # noqa: BLE001
                return False
        return bool(self._run_js(body))

    def press_if_present(self, key: str) -> None:
        log.debug("press_if_present(%s) non supporte par le moteur botasaurus", key)

    # -- interaction (drivers) ------------------------------------------------

    def click_any(self, selectors: Sequence[str], timeout_ms: int = 4000) -> Optional[str]:
        for raw in selectors:
            try:
                sel = translate_selector(raw)
                if self._present(sel, wait_s=timeout_ms / 1000):
                    self.driver.click(sel.css, wait=max(1, timeout_ms // 1000))
                    return raw
            except Exception:  # noqa: BLE001
                continue
        return None

    def type_into(self, selectors: Sequence[str], text: str) -> Optional[str]:
        for raw in selectors:
            try:
                sel = translate_selector(raw)
                if not self._present(sel, wait_s=1.0):
                    continue
                self.driver.click(sel.css, wait=2)
                try:
                    self.driver.clear(sel.css, wait=1)
                except Exception:  # noqa: BLE001
                    pass
                self.driver.type(sel.css, text, wait=2)
                return raw
            except Exception:  # noqa: BLE001
                continue
        return None

    def upload_any(self, selectors: Sequence[str], paths: Sequence[Path | str]) -> Optional[str]:
        files = [str(path) for path in paths]
        for raw in selectors:
            try:
                sel = translate_selector(raw)
                if not self._present(sel, wait_s=1.0):
                    continue
                if len(files) == 1:
                    self.driver.upload_file(sel.css, files[0], wait=5)
                else:
                    self.driver.upload_multiple_files(sel.css, files, wait=5)
                return raw
            except Exception:  # noqa: BLE001
                continue
        return None

    def press(self, key: str) -> None:
        log.debug("press(%s) non supporte directement par botasaurus", key)

    def is_input_empty(self, selectors: Sequence[str]) -> bool:
        """Vrai si le premier champ de saisie trouve est vide (envoi accepte)."""
        for raw in selectors:
            sel = translate_selector(raw)
            if not self._present(sel, wait_s=0.0):
                continue
            empty = self._run_js(
                "const el = document.querySelector(%s);"
                "if (!el) return true;"
                "const v = (el.value !== undefined && el.value !== null) ? el.value "
                ": (el.innerText || el.textContent || '');"
                "return String(v).trim().length === 0;" % json.dumps(sel.css)
            )
            return bool(empty)
        return True

    # -- scroll ------------------------------------------------------------------

    def scroll_page_until_stable(
        self,
        max_rounds: int = 60,
        pause_ms: int = 700,
        stable_rounds: int = 3,
    ) -> ScrollResult:
        last_height = -1
        stable = 0
        rounds = 0
        for i in range(max_rounds):
            rounds = i + 1
            self._run_js("window.scrollTo(0, document.body.scrollHeight)")
            self.wait_ms(pause_ms)
            height = self._run_js(
                "return Math.max(document.body.scrollHeight, "
                "document.documentElement.scrollHeight)"
            )
            at_bottom = self._run_js(
                "return (window.innerHeight + window.scrollY) >= "
                "document.body.scrollHeight - 8"
            )
            height = int(height or 0)
            if height == last_height:
                stable += 1
            else:
                stable = 0
            last_height = height
            if at_bottom and stable >= stable_rounds:
                return ScrollResult(True, rounds, height)
        return ScrollResult(False, rounds, max(0, last_height))

    def scroll_element_until_stable(
        self,
        selectors: Sequence[str],
        max_rounds: int = 25,
        pause_ms: int = 500,
        stable_rounds: int = 2,
    ) -> ScrollResult:
        body = None
        for raw in selectors:
            candidate = js_scroll_body(translate_selector(raw))
            if candidate is not None:
                body = candidate
                break
        if body is None:
            return ScrollResult(True, 0, 0)
        last_height = -1
        stable = 0
        height = 0
        for i in range(max_rounds):
            res = self._run_js(body)
            self.wait_ms(pause_ms)
            if not res:
                return ScrollResult(True, i + 1, 0)  # conteneur absent : rien a scroller
            height, reached = int(res[0] or 0), bool(res[1])
            if height == last_height:
                stable += 1
            else:
                stable = 0
            last_height = height
            if reached and stable >= stable_rounds:
                return ScrollResult(True, i + 1, height)
        return ScrollResult(False, max_rounds, height)

    # -- diagnostics ---------------------------------------------------------------

    def screenshot(self, path: Path | str) -> Optional[Path]:
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.driver.save_screenshot(str(target))
            return target
        except Exception as exc:  # noqa: BLE001
            log.debug("screenshot failed: %s", exc)
            return None

    def looks_logged_out(
        self, login_url_parts: List[str], login_selectors: List[str]
    ) -> bool:
        url = (self.url() or "").lower()
        if any(part in url for part in login_url_parts):
            return True
        for raw in login_selectors:
            if self._present(translate_selector(raw), wait_s=0.0):
                return True
        return False

    def looks_blocked(self) -> Optional[str]:
        """Detecte un challenge anti-bot (Cloudflare Turnstile, case a cocher)."""
        try:
            if self.driver.is_bot_detected_by_cloudflare():
                return "cloudflare-driver"
        except Exception:  # noqa: BLE001
            pass
        body = (self.evaluate("return document.body ? document.body.innerText : '';") or "")
        text = str(body).lower()
        if "verify you are human" in text:
            return "verify-human"
        if "performing security verification" in text:
            return "security-verification"
        if "just a moment" in text:
            return "just-a-moment"
        tl = self.title().lower()
        if "just a moment" in tl or "attention! verified by cloudflare" in tl:
            return "cloudflare-title"
        iframe = self.evaluate(
            "return !!document.querySelector("
            "\"iframe[src*='challenges.cloudflare.com'], "
            "iframe[src*='cloudflare-tds'], #challenge-stage\");"
        )
        if iframe:
            return "cloudflare"
        return None

    def try_solve_cloudflare(self, wait_ms: int = 6000) -> bool:
        """Laisse botasaurus resoudre le challenge. Retourne True si passe."""
        try:
            self.driver.detect_and_bypass_cloudflare()
        except Exception as exc:  # noqa: BLE001
            log.debug("detect_and_bypass_cloudflare: %s", exc)
        self.wait_ms(wait_ms)
        return self.looks_blocked() is None

    def interactive_login(self, service_name: str, login_url: str) -> None:
        """Ouvre un navigateur visible pour connexion manuelle (profil persistant)."""
        self.headless = False
        self.start()
        if login_url:
            self.goto(login_url)
        log.info(
            "Navigateur ouvert pour connexion %s — connectez-vous puis "
            "fermez le processus quand termine (Ctrl+C).",
            service_name,
        )
        login_selectors = [
            "a[href*='login' i]",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
            "input[type='password']",
        ]
        try:
            while True:
                self.wait_ms(3000)
                url = self.url().lower()
                still_login = any(
                    part in url
                    for part in ("login", "signin", "sign-in", "auth", "accounts")
                )
                if not still_login and not any(
                    self._present(translate_selector(s), wait_s=0.0)
                    for s in login_selectors
                ):
                    break
        except KeyboardInterrupt:
            pass
        cookies = []
        try:
            cookies = self.driver.get_cookies() or []
        except Exception:  # noqa: BLE001
            pass
        log_fields(
            log, 20, "login termine",
            extra={"service": service_name, "cookies": len(cookies)},
        )
