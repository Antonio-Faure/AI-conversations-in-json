"""Wrapper Playwright : profil persistant par service, navigation, attente, scroll.

Un BrowserSession encapsule un `launch_persistent_context` Chromium (cookies et
sessions conservees entre les executions dans `<profile_dir>/<service>/`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from .utils.logging import get_logger, log_fields

log = get_logger("browser")

DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

#: masque les marqueurs d'automatisation les plus courants (headless detection)
STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p);
}
"""


@dataclass
class ScrollResult:
    reached_bottom: bool
    rounds: int
    last_height: int


class BrowserSession:
    """Session navigateur avec profil persistant (contexte sync Playwright)."""

    def __init__(
        self,
        profile_dir: Optional[Path | str] = None,
        headless: bool = False,
        timeout_ms: int = 45000,
        nav_timeout_ms: Optional[int] = None,
        viewport: Optional[Dict[str, int]] = None,
        channel: Optional[str] = None,
        service: Optional[str] = None,
        user_data_dir: Optional[Path | str] = None,
    ):
        if user_data_dir is not None:
            self.profile_dir = Path(user_data_dir)
        elif profile_dir is not None:
            self.profile_dir = Path(profile_dir)
        else:
            self.profile_dir = Path("profiles")
        self.headless = headless
        self.timeout_ms = int(nav_timeout_ms or timeout_ms)
        self.viewport = viewport or DEFAULT_VIEWPORT
        self.channel = channel
        self.service = service
        self._pw = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # -- cycle de vie -------------------------------------------------------

    def start(self) -> "BrowserSession":
        if self._context is not None:
            return self
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "viewport": self.viewport,
            "locale": "en-US",
            "ignore_default_args": ["--enable-automation"],
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.channel:
            launch_kwargs["channel"] = self.channel
        # UA identique en headful et headless : indispensable pour que les
        # cookies Cloudflare (cf_clearance, lie a l'UA) survivent entre la
        # connexion (--login) et l'export headless quotidien.
        try:
            chrome_major = self._pw.chromium.version.split(".")[0]
            launch_kwargs["user_agent"] = USER_AGENT_TEMPLATE.format(major=chrome_major)
        except Exception:  # noqa: BLE001
            launch_kwargs["user_agent"] = USER_AGENT
        self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self._context.set_default_timeout(self.timeout_ms)
        self._context.set_default_navigation_timeout(self.timeout_ms)
        try:
            self._context.add_init_script(STEALTH_JS)
        except Exception:  # noqa: BLE001
            log.debug("stealth init script skipped", exc_info=True)
        log_fields(
            log,
            20,
            "browser started",
            extra={"profile": str(self.profile_dir), "headless": self.headless},
        )
        return self

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                pass
            self._context = None
            self._page = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- page ---------------------------------------------------------------

    @property
    def page(self) -> Page:
        self.start()
        assert self._context is not None
        if self._page is None or self._page.is_closed():
            pages = [p for p in self._context.pages if not p.is_closed()]
            self._page = pages[0] if pages else self._context.new_page()
        return self._page

    # -- navigation ----------------------------------------------------------

    def goto(self, url: str, wait_until: str = "domcontentloaded") -> Page:
        page = self.page
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                page.goto(url, wait_until=wait_until)
                return page
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                last_error = exc
                log_fields(
                    log,
                    30,
                    "navigation retry",
                    extra={"url": url, "attempt": attempt + 1, "error": str(exc)},
                )
                page.wait_for_timeout(1000 * (attempt + 1))
        raise RuntimeError(f"navigation impossible vers {url}: {last_error}")

    def reload(self) -> None:
        """Recharge la page courante (facade commune aux deux moteurs)."""
        try:
            self.page.reload(wait_until="domcontentloaded")
        except PlaywrightError as exc:
            log.debug("reload failed: %s", exc)

    def wait_ms(self, ms: int) -> None:
        """Attente passive (facade commune aux deux moteurs)."""
        try:
            self.page.wait_for_timeout(ms)
        except PlaywrightError:
            pass

    def wait_for_any(
        self,
        selectors: Sequence[str],
        timeout_ms: Optional[int] = None,
    ) -> Optional[str]:
        """Attend le premier selecteur visible parmi `selectors`. Retourne celui-ci."""
        page = self.page
        per = max(1500, (timeout_ms or self.timeout_ms) // max(1, len(selectors)))
        for sel in selectors:
            try:
                page.wait_for_selector(sel, state="attached", timeout=per)
                return sel
            except PlaywrightTimeoutError:
                continue
        return None

    def html(self) -> str:
        return self.page.content()

    def url(self) -> str:
        return self.page.url

    def evaluate(self, script: str, arg: Any = None) -> Any:
        try:
            return self.page.evaluate(script, arg)
        except PlaywrightError as exc:
            log.debug("evaluate failed: %s", exc)
            return None

    def click_if_present(self, selector: str, timeout_ms: int = 2500) -> bool:
        try:
            self.page.click(selector, timeout=timeout_ms)
            return True
        except (PlaywrightTimeoutError, PlaywrightError):
            return False

    def is_element_present(self, selector: str) -> bool:
        """Presence immediate d'un element (facade commune aux deux moteurs)."""
        try:
            return self.page.query_selector(selector) is not None
        except PlaywrightError:
            return False

    def press_if_present(self, key: str) -> None:
        try:
            self.page.keyboard.press(key)
        except PlaywrightError:
            pass

    # -- scroll ---------------------------------------------------------------

    def scroll_page_until_stable(
        self,
        max_rounds: int = 60,
        pause_ms: int = 700,
        stable_rounds: int = 3,
    ) -> ScrollResult:
        """Scrolle la fenetre jusqu'a ce que la hauteur de page ne bouge plus.

        Gere le scroll infini des fils de discussion (chargement progressif des
        anciens/autres messages).
        """
        page = self.page
        last_height = -1
        stable = 0
        rounds = 0
        for i in range(max_rounds):
            rounds = i + 1
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(pause_ms)
            height = page.evaluate(
                "Math.max(document.body.scrollHeight, "
                "document.documentElement.scrollHeight)"
            )
            at_bottom = page.evaluate(
                "(window.innerHeight + window.scrollY) >= "
                "document.body.scrollHeight - 8"
            )
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
        """Scrolle un conteneur (sidebar) jusqu'a stabilisation de scrollHeight."""
        page = self.page
        handle = None
        for sel in selectors:
            try:
                handle = page.query_selector(sel)
            except PlaywrightError:
                handle = None
            if handle is not None:
                break
        if handle is None:
            return ScrollResult(True, 0, 0)
        last_height = -1
        stable = 0
        height = 0
        for i in range(max_rounds):
            handle.evaluate(
                "(el) => el.scrollBy({top: el.clientHeight || 400, behavior: 'instant'})"
            )
            page.wait_for_timeout(pause_ms)
            height = handle.evaluate("el => el.scrollHeight")
            reached = handle.evaluate(
                "el => el.scrollTop + el.clientHeight >= el.scrollHeight - 8"
            )
            if height == last_height:
                stable += 1
            else:
                stable = 0
            last_height = height
            if reached and stable >= stable_rounds:
                return ScrollResult(True, i + 1, height)
        return ScrollResult(False, max_rounds, height)

    # -- diagnostics -----------------------------------------------------------

    def screenshot(self, path: Path | str) -> Optional[Path]:
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target), full_page=False)
            return target
        except PlaywrightError as exc:
            log.debug("screenshot failed: %s", exc)
            return None

    def looks_logged_out(self, login_url_parts: List[str], login_selectors: List[str]) -> bool:
        """True si la page courante ressemble a un ecran de connexion."""
        url = self.url().lower()
        if any(part in url for part in login_url_parts):
            return True
        for sel in login_selectors:
            try:
                if self.page.query_selector(sel):
                    return True
            except PlaywrightError:
                continue
        return False

    def looks_blocked(self) -> Optional[str]:
        """Detecte un challenge anti-bot (Cloudflare Turnstile, case a cocher).

        Retourne un libelle du motif rencontre, sinon None. Le texte du
        widget Turnstile vit dans une iframe cross-origin : on scanne aussi
        tous les frames.
        """
        markers = {
            "cloudflare": "iframe[src*='challenges.cloudflare.com']",
            "cloudflare-tds": "iframe[src*='cloudflare-tds'], #challenge-stage",
            "verify-human": "text=/verify you are human/i",
            "security-verification": "text=/performing security verification/i",
            "just-a-moment": "text=/just a moment/i",
        }
        for label, sel in markers.items():
            try:
                if self.page.query_selector(sel):
                    return label
            except PlaywrightError:
                continue
        for frame in self.page.frames:
            try:
                body = (frame.text_content("body") or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if "verify you are human" in body or "security verification" in body:
                return "cloudflare-frame"
        title = ""
        try:
            title = (self.page.title() or "").lower()
        except PlaywrightError:
            pass
        if "just a moment" in title or "attention! verified by cloudflare" in title:
            return "cloudflare-title"
        return None

    def try_solve_cloudflare(self, wait_ms: int = 6000) -> bool:
        """Tente de cocher la case Turnstile. Retourne True si passe."""
        try:
            box = (
                self.page.frame_locator("iframe[src*='challenges.cloudflare.com']")
                .locator("input#challenge-turnstile-checkbox, input[type='checkbox']")
                .first
            )
            box.click(timeout=5000)
            self.page.wait_for_timeout(wait_ms)
            return self.looks_blocked() is None
        except Exception:  # noqa: BLE001
            return False

    def interactive_login(self, service_name: str, login_url: str) -> None:
        """Ouvre le navigateur visible pour que l'utilisateur se connecte.

        Attend que l'utilisateur navigue loin de la page de login (detected par
        l'absence des selecteurs de login) puis sauvegarde le profil.
        """
        self.start()
        page = self.page
        if login_url:
            page.goto(login_url, wait_until="domcontentloaded")
        log.info(
            "Navigateur ouvert pour connexion %s — connectez-vous puis "
            "fermez le navigateur quand termine.",
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
                page.wait_for_timeout(3000)
                url = page.url.lower()
                still_login = any(
                    part in url
                    for part in ("login", "signin", "sign-in", "auth", "accounts")
                )
                if not still_login:
                    has_login_el = False
                    for sel in login_selectors:
                        try:
                            if page.query_selector(sel):
                                has_login_el = True
                                break
                        except PlaywrightError:
                            pass
                    if not has_login_el:
                        break
        except KeyboardInterrupt:
            pass
