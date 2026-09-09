"""Classe abstraite BaseService : decouverte + scraping via Playwright.

Un service connait son/ses URL, ses selecteurs de sidebar et delague tout le
travail de lecture DOM a son parser (BeautifulSoup, testable sans navigateur).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin

from ..parsers.base import LOGIN_URL_PARTS, ParseError
from ..schema import Conversation, ConversationRef
from ..selectors import translate_selector
from ..utils.logging import get_logger, log_fields

if TYPE_CHECKING:  # evite la dependance playwright pour les tests parsers/schema
    from ..browser import BrowserSession

log = get_logger("services")


class ServiceNotLoggedIn(Exception):
    """Session absente/expirée : ouvrir le service en mode visible pour se connecter."""


class BlockedError(RuntimeError):
    """Challenge anti-bot (Cloudflare) : passer une fois en --headful pour valider."""


class EmptyConversationError(RuntimeError):
    """La conversation ne charge aucun message (vide, tâche, ou app en echec)."""


#: textes des dialogs de rate limite (ChatGPT "Too many requests", etc.)
RATE_LIMIT_MARKERS = ("too many requests", "trop de requ", "temporairement limit")
RATE_LIMIT_DISMISS_SELECTORS = (
    "button:has-text('Got it')",
    "button:has-text('OK')",
    "button:has-text('J'ai compris')",
)


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
    #: page de decouverte (liste des conversations) si differente de home_url
    discover_url: str = ""
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
    #: marqueurs d'ecran vide (conversation non chargeable : tache, supprimee...)
    empty_chat_selectors: tuple = ()

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
        cfg = self.config.get("sidebar", {})
        return {
            "max_rounds": int(cfg.get("max_rounds", 25)),
            "pause_ms": int(cfg.get("pause_ms", 500)),
            "stable_rounds": int(cfg.get("stable_rounds", 2)),
        }

    def after_sidebar_open(self) -> None:
        """Hook apres ouverture de la home (ex: cliquer 'Show all' sur Gemini)."""

    def discover_via_api(self) -> Optional[List[ConversationRef]]:
        """Decouverte par API du service (surcharge). None = fallback DOM."""
        return None

    def _is_rate_limited(self) -> bool:
        """True si la page affiche un dialog de rate limite."""
        try:
            text = (self.session.evaluate(
                "return document.body ? document.body.innerText : '';"
            ) or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(marker in text for marker in RATE_LIMIT_MARKERS)

    def _wait_out_rate_limit(self, waits_s: Sequence[int] = (15, 30, 60)) -> bool:
        """Ferme le dialog et attend que la limite se leve. True si limite vue."""
        limited = False
        for wait_s in waits_s:
            if not self._is_rate_limited():
                return limited
            limited = True
            log_fields(
                log, 30, f"{self.name}: rate limite detecte -> attente {wait_s}s",
            )
            for sel in RATE_LIMIT_DISMISS_SELECTORS:
                if self.session.click_if_present(sel, timeout_ms=1500):
                    break
            self.session.wait_ms(wait_s * 1000)
        return limited

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
        """Ouvre la home, scroll la sidebar, extrait les refs via le parser.

        Le lazy-load des sidebars est capricieux (liste partielle/vide) :
        si 0 ref est extraite, on recharge et retente (jusqu'a 3 essais).
        """
        refs = self._list_once(limit=limit)
        for wait_s in (3, 30, 60):
            if refs:
                break
            if self._is_rate_limited():
                # "Too many requests" : attendre la levee de la limite
                # (recharger tout de suite ne sert a rien)
                self._wait_out_rate_limit()
            log_fields(
                log, 30,
                f"{self.name}: 0 conversation -> rechargement + nouvelle tentative "
                f"(attente {wait_s}s)",
            )
            self.session.reload()
            self.session.wait_ms(wait_s * 1000)
            refs = self._list_once(limit=limit)
        if limit:
            refs = refs[:limit]
        return refs

    def _list_once(self, limit: Optional[int] = None) -> List[ConversationRef]:
        discover_url = self.discover_url or self.home_url
        self.session.goto(discover_url)
        self._assert_not_blocked("home")
        if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
            raise ServiceNotLoggedIn(
                f"{self.name}: pas de session valide -> lancer une execution "
                f"headless=false et se connecter sur {self.home_url}"
            )
        self.session.wait_for_any(list(self.sidebar_ready_selectors) or list(self.parser.link_selectors))
        self.after_sidebar_open()
        api_refs = self.discover_via_api()
        if api_refs:
            refs = {}
            for ref in api_refs:
                refs[ref.id] = ref
        else:
            refs = self._collect_sidebar_refs()
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
        return list(refs.values())

    # -- collecte incrementale de la liste (listes virtualisees) ----------------

    def _link_selector_css(self) -> str:
        """Selecteurs CSS purs des liens de conversation (sans pseudo Playwright)."""
        parts = []
        for raw in self.parser.link_selectors:
            sel = translate_selector(raw)
            if sel.plain and sel.css:
                parts.append(sel.css)
        return ", ".join(parts) if parts else "a[href]"

    def _scroll_list_js(self) -> str:
        """JS sautant au bas de tous les conteneurs scrollables.

        Les listes virtualisees (Perplexity) rendent les items dans un overlay
        sibling du vrai scroller, et Gemini utilise des composants custom
        (infinite-scroller) : impossible d'identifier le bon conteneur par
        tag ou par liens -> on scrolle tout element scrollable.
        """
        return (
            "let jumped = 0;"
            "for (const e of document.querySelectorAll('*')) {"
            "  if (e.scrollHeight <= e.clientHeight + 50 || e.clientHeight < 150) continue;"
            "  e.scrollTop = e.scrollHeight;"
            "  jumped++;"
            "}"
            "window.scrollTo(0, document.body.scrollHeight);"
            "return jumped;"
        )

    def _collect_refs_js(self) -> str:
        sel = json.dumps(self._link_selector_css())
        return (
            "return Array.from(document.querySelectorAll(" + sel + "))"
            ".map(a => [a.getAttribute('href') || '', (a.textContent || '').trim()]);"
        )

    def _collect_sidebar_refs(self) -> Dict[str, ConversationRef]:
        """Scrolle la liste en accumulant les refs (listes virtualisees : les
        elements hors viewport disparaissent du DOM, il faut collecter a
        chaque tour de scroll)."""
        cfg = self._scroll_config()
        refs: Dict[str, ConversationRef] = {}
        pattern = self.parser.conversation_id_pattern
        last_total = -1
        stable = 0
        max_rounds = max(30, cfg["max_rounds"])
        for _ in range(max_rounds):
            self.session.eval_body(self._scroll_list_js())
            self.session.wait_ms(cfg["pause_ms"])
            rows = self.session.eval_body(self._collect_refs_js()) or []
            for href, text in rows:
                match = pattern.search(href or "")
                if not match:
                    continue
                cid = match.group(1)
                if cid not in refs:
                    refs[cid] = ConversationRef(
                        service=self.parser.service_name,
                        id=cid,
                        url=urljoin(self.home_url, href),
                        title=text[:120] or None,
                    )
            if len(refs) == last_total:
                stable += 1
            else:
                stable = 0
            last_total = len(refs)
            if refs and stable >= max(2, cfg["stable_rounds"]):
                break
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
            # seconde chance : SPA lente / rate limite / rendu differe
            if self._is_rate_limited():
                self._wait_out_rate_limit()
            log_fields(
                log, 30,
                f"{self.name}: aucun message vu sur {url}, rechargement...",
            )
            self.session.reload()
            self.session.wait_ms(3000)
            # budget double : les fils volumineux rendent lentement
            retry_budget = int(getattr(self.session, "timeout_ms", 45000)) * 2
            matched = self.session.wait_for_any(
                list(self.parser.message_selectors), timeout_ms=retry_budget
            )
        if matched is None:
            for sel in self.empty_chat_selectors:
                if self.session.is_element_present(sel):
                    raise EmptyConversationError(
                        f"{self.name}: conversation sans message chargeable sur {url} "
                        f"(vide, tache, ou echec de rendu de l'app)"
                    )
            shot = self.session.screenshot(
                Path(self.config.get("screenshot_dir", ".")) / f"debug_{self.name}_conv.png"
            )
            raise ParseError(
                f"{self.name}: aucun message reconnu sur {url} "
                f"(DOM change ? selecteurs: {self.parser.message_selectors})"
                + (f" [capture: {shot}]" if shot else "")
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
