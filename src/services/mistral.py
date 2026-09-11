"""Scraper Mistral (chat.mistral.ai) via navigateur (botasaurus anti-Cloudflare).

Mistral a deux modes a scraper :
  - /chat  : chatbot (historique principal)
  - /work  : mode agentique

Le contenu d'une conversation n'est rendu qu'apres activation du mode via
l'« app switcher » ; `_ensure_mode` s'en charge avant chaque navigation.
L'authentification se fait par les cookies du profil persistant
(`profiles/mistral`, via `run.py --login mistral`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..parsers.mistral import MistralParser
from ..schema import ConversationRef
from ..services.base import BaseService, ServiceNotLoggedIn

BASE = "https://chat.mistral.ai"
MODES = ("chat", "work")


class MistralService(BaseService):
    name = "mistral"
    home_url = BASE + "/work"

    sidebar_scroll_selectors = (
        "div.sidebar-dynamic-scroll-area-viewport",
        "div.overflow-y-auto",
        "aside",
    )
    sidebar_ready_selectors = ("a[href^='/chat/']", "a[href^='/work/']")
    # pas de selecteur href*=login : les conversations peuvent contenir de tels
    # liens (faux positif -> "session perdue"). L'URL de login suffit.
    login_url_parts = (
        "chat.mistral.ai/login",
        "chat.mistral.ai/auth",
        "signin",
        "login",
        "accounts.google",
    )
    login_selectors = (
        "button:has-text('Sign in')",
        "button:has-text('Se connecter')",
        "input[type='password']",
    )

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        self._mode: Optional[str] = None

    def build_parser(self) -> MistralParser:
        return MistralParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        mode = (ref.raw or {}).get("mode", "work")
        return ref.url or f"{BASE}/{mode}/{ref.id}"

    # -- activation du mode via l'app switcher ---------------------------------

    def _open_selected_app(self, mode: str) -> None:
        """Ouvre l'app switcher puis clique le lien du mode."""
        opened = self.session.eval_body(
            "const b=[...document.querySelectorAll('button')].find("
            "x=>/app switcher/i.test(x.getAttribute('aria-label')||''));"
            "if(b){b.click();return true}return false;"
        )
        if not opened:
            return
        self.session.wait_ms(1200)
        self.session.eval_body(
            "const a=document.querySelector(\"a[href='/%s']\");"
            "if(a){a.click();return true}return false;" % mode
        )

    def _ensure_mode(self, mode: str) -> None:
        """Charge le shell puis active le mode demande (chat|work)."""
        if self._mode == mode:
            return
        self.session.goto(self.home_url)
        self.session.wait_ms(3000)
        if mode != "work":
            self._open_selected_app(mode)
            self.session.wait_ms(4000)
        self._mode = mode

    # -- decouverte (chat + work) ----------------------------------------------

    def _discover_mode(self, mode: str) -> Dict[str, ConversationRef]:
        collected: Dict[str, ConversationRef] = {}
        for attempt in range(3):
            self._ensure_mode(mode)
            self.session.wait_ms(2000 if attempt == 0 else 3000)
            self._assert_not_blocked(f"mistral/{mode}")
            if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
                raise ServiceNotLoggedIn(
                    "mistral: pas de session valide -> `run.py --login mistral`"
                )
            refs = self._collect_sidebar_refs()
            collected = {
                cid: ref
                for cid, ref in refs.items()
                if f"/{mode}/" in (ref.url or "")
            }
            if collected:
                break
        for ref in collected.values():
            ref.raw = dict(ref.raw or {})
            ref.raw["mode"] = mode
        return collected

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        refs: Dict[str, ConversationRef] = {}
        for mode in MODES:
            for cid, ref in self._discover_mode(mode).items():
                refs.setdefault(cid, ref)
            if limit and len(refs) >= limit:
                break
        return list(refs.values())

    # -- scraping --------------------------------------------------------------

    def scrape_conversation(self, ref: ConversationRef):
        self._ensure_mode((ref.raw or {}).get("mode", "work"))
        return super().scrape_conversation(ref)
