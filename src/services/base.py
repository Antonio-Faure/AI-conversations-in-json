"""Classe abstraite BaseService : decouverte + scraping via Playwright.

Un service connait son/ses URL, ses selecteurs de sidebar et delague tout le
travail de lecture DOM a son parser (BeautifulSoup, testable sans navigateur).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..parsers.base import LOGIN_URL_PARTS, ParseError
from ..schema import Conversation, ConversationRef
from ..utils.logging import get_logger, log_fields

if TYPE_CHECKING:  # evite la dependance playwright pour les tests parsers/schema
    from ..browser import BrowserSession

log = get_logger("services")


class ServiceNotLoggedIn(Exception):
    """Session absente/expirée : ouvrir le service en mode visible pour se connecter."""


class BlockedError(RuntimeError):
    """Challenge anti-bot (Cloudflare) : passer une fois en --headful pour valider."""


@dataclass
class ScrapedPage:
    html: str
    conversation_id: str
    url: str
    extra: Dict[str, Any] = field(default_factory=dict)


class BaseService(ABC):
    """Pipeline commun : list_conversations -> scrape_conversation -> parse."""

    name: str = ""
    home_url: str = ""
    #: selectors des conteneurs scrollables de la sidebar
    sidebar_scroll_selectors: tuple = ()
    #: selectors qui prouvent que la liste de conversations est chargee
    sidebar_ready_selectors: tuple = ()
    #: parties d'URL de login specifiques au service (complete LOGIN_URL_PARTS)
    login_url_parts: tuple = LOGIN_URL_PARTS
    #: selectors d'ecran de login (bouton "Log in"/"Sign in")
    login_selectors: tuple = (
        "a[href*='login' i]",
        "button:has-text('Log in')",
        "input[type='password']",
    )

    def __init__(self, session: BrowserSession, config: Optional[Dict[str, Any]] = None):
        self.session = session
        self.config = config or {}
        self.parser = self.build_parser()

    # -- a implementer par service ---------------------------------------------

    @abstractmethod
    def build_parser(self):
        """Retourne une instance du parser DOM du service."""

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        """Donnees non-DOM (ex: timestamps reactFiber ChatGPT). Defaut: vide."""
        return {}

    # -- pipeline commun ----------------------------------------------------------

    def _scroll_config(self) -> Dict[str, int]:
        return {
            "max_rounds": int(self.config.get("sidebar", {}).get("max_rounds", 25)),
            "pause_ms": int(self.config.get("sidebar", {}).get("pause_ms", 500)),
        }

    def after_sidebar_open(self) -> None:
        """Hook apres ouverture de la home (ex: cliquer 'Show all' sur Gemini)."""

    def _assert_not_blocked(self, where: str) -> None:
        """Détecte un challenge anti-bot, tente un auto-click, puis leve BlockedError."""
        marker = self.session.looks_blocked()
        if not marker:
            return
        log_fields(log, 30, f"{self.name}: challenge anti-bot ({marker}) sur {where}, tentative de clic...",
                   extra={"service": self.name, "marker": marker})
        if self.session.try_solve_cloudflare():
            log_fields(log, 20, f"{self.name}: challenge resolu automatiquement",
                       extra={"service": self.name})
            return
        # certaines pages se resorbent apres un rechargement
        self.session.reload()
        self.session.wait_ms(2500)
        marker = self.session.looks_blocked()
        if not marker:
            return
        shot = None
        try:
            shot = self.session.screenshot(
                Path(self.config.get("screenshot_dir", ".")) / f"blocked_{self.name}.png"
            )
        except Exception:  # noqa: BLE001
            pass
        raise BlockedError(
            f"{self.name}: bloque par un challenge anti-bot ({marker}) sur {where}. "
            f"Solution: lancer une fois `python run.py --login {self.name}` (mode "
            f"visible) et cocher la case Cloudflare ; le cookie est alors "
            f"reutilise en headless." + (f" [capture: {shot}]" if shot else "")
        )

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        """Ouvre la home, scroll la sidebar, extrait les refs via le parser."""
        self.session.goto(self.home_url)
        self._assert_not_blocked("home")
        if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
            raise ServiceNotLoggedIn(
                f"{self.name}: pas de session valide -> lancer une execution "
                f"headless=false et se connecter sur {self.home_url}"
            )
        self.session.wait_for_any(list(self.sidebar_ready_selectors) or list(self.parser.link_selectors))
        self.after_sidebar_open()
        sidebar_cfg = self._scroll_config()
        self.session.scroll_element_until_stable(
            list(self.sidebar_scroll_selectors),
            max_rounds=sidebar_cfg["max_rounds"],
            pause_ms=sidebar_cfg["pause_ms"],
        )
        refs = self.parser.parse_links(self.session.html(), self.home_url)
        if not refs:
            # le challenge Cloudflare peut apparaitre apres le goto : on
            # re-verifie avant de conclure a un DOM vide
            self._assert_not_blocked("home (post-listing)")
            shot = self.session.screenshot(Path(self.config.get("screenshot_dir", ".")) / f"debug_{self.name}_home.png")
            log_fields(
                log,
                30,
                f"{self.name}: 0 conversation detectee (session expirree ? DOM "
                f"change ? capture: {shot})",
                extra={"count": 0, "home_url": self.home_url},
            )
        log_fields(
            log,
            20,
            f"{self.name}: conversations trouvees",
            extra={"count": len(refs), "limit": limit},
        )
        if limit:
            refs = refs[:limit]
        return refs

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge une conversation, scroll jusqu'au bout, capture HTML + extras."""
        url = self.conversation_url(ref)
        self.session.goto(url)
        self._assert_not_blocked(f"conversation {ref.id}")
        if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
            raise ServiceNotLoggedIn(f"{self.name}: session perdue sur {url}")
        matched = self.session.wait_for_any(list(self.parser.message_selectors))
        if matched is None:
            raise ParseError(
                f"{self.name}: aucun message reconnu sur {url} "
                f"(DOM change ? selecteurs: {self.parser.message_selectors})"
            )
        scroll_cfg = self.config.get("scroll", {})
        self.session.scroll_page_until_stable(
            max_rounds=int(scroll_cfg.get("max_rounds", 60)),
            pause_ms=int(scroll_cfg.get("pause_ms", 700)),
            stable_rounds=int(scroll_cfg.get("stable_rounds", 3)),
        )
        return ScrapedPage(
            html=self.session.html(),
            conversation_id=ref.id,
            url=url,
            extra=self.extract_extras(ref),
        )

    def export_conversation(self, ref: ConversationRef) -> Conversation:
        page = self.scrape_conversation(ref)
        conv = self.parser.parse(
            page.html, conversation_id=page.conversation_id, extra=page.extra
        )
        conv.service = self.name
        if not conv.conversation_id:
            conv.conversation_id = ref.id
        return conv
