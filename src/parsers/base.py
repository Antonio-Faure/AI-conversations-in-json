"""Classe abstraite BaseParser : HTML -> Conversation du schema standardise.

Les parsers travaillent sur des chaines HTML (issues de `page.content()` ou de
fixtures) avec BeautifulSoup et des selecteurs CSS — les memes selecteurs que
les services passent a Playwright pour les attentes/scrolls.
"""

from __future__ import annotations

import copy
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from ..schema import Conversation, ConversationRef, Message, SchemaError
from ..utils.logging import get_logger, log_fields

log = get_logger("parsers")

ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
SPACE_RE = re.compile(r"[ \t]+")
NEWLINES_RE = re.compile(r"\n{3,}")

# Selecteurs CSS des listes de conversations (surcharge par service)
LOGIN_URL_PARTS = (
    "accounts.google.com",
    "chatgpt.com/auth",
    "claude.ai/__clerc",
    "perplexity.ai/.auth",
    "/sign-in",
    "/login",
    "/oauth",
    "/billing",
)

DROP_TAGS = ("script", "style", "noscript", "iframe", "button", "svg", "textarea")

#: caracteres de controle de direction (RTL/LTR) injectes par certaines UIs
DIRECTION_MARKS_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def clean_ui_title(title: Optional[str]) -> Optional[str]:
    """Titre utilisable : sans marques de direction, nbsp, ni libelle generique."""
    if not title:
        return None
    title = DIRECTION_MARKS_RE.sub("", title).replace("\u00a0", " ").strip()
    title = re.sub(r"\s+", " ", title)
    if not title or title.lower() in ("google gemini", "gemini", "perplexity", "chatgpt"):
        return None
    return title
ACTION_SELECTORS = (
    "button",
    "[role='button']",
    ".action-bar",
    "message-actions",
    "[data-testid$='-menu']",
    "[aria-label*='Copy' i]",
    "[aria-label*='Regenerate' i]",
    "[aria-label*='like' i]",
    # UI invisible hors hover (timestamps de bulle, labels decoratifs)
    "span[class*='opacity-0']",
    "[aria-hidden='true']",
)


class ParseError(Exception):
    """HTML de conversation non reconnaissable (DOM change, login wall...)."""


class BaseParser(ABC):
    """Interface commune des parsers DOM par plateforme."""

    service_name: str = ""
    #: selecteurs CSS des liens de conversation dans la sidebar
    link_selectors: tuple = ()
    #: regex pour extraire l'id depuis l'href des liens
    conversation_id_pattern: re.Pattern = re.compile(r"/[a-z-]+/([A-Za-z0-9_-]{6,})")
    #: selecteurs d'un message (utilises par le service pour attendre le rendu)
    message_selectors: tuple = ()

    # -- API publique ---------------------------------------------------------

    @abstractmethod
    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        """HTML d'une page de conversation -> Conversation validee."""

    def parse_links(self, html: str, base_url: str) -> List[ConversationRef]:
        """Extrait les references de conversations d'une sidebar."""
        soup = self.make_soup(html)
        refs: List[ConversationRef] = []
        seen: set = set()
        for sel in self.link_selectors:
            for a in soup.select(sel):
                href = a.get("href") or a.get("data-href") or ""
                match = self.conversation_id_pattern.search(href)
                if not match:
                    continue
                conv_id = match.group(1)
                if conv_id in seen:
                    continue
                seen.add(conv_id)
                title = self._link_title(a)
                section = self._link_section(a)
                refs.append(
                    ConversationRef(
                        service=self.service_name,
                        id=conv_id,
                        url=urljoin(base_url, href),
                        title=title,
                        section=section,
                    )
                )
        return refs

    # -- aides BeautifulSoup -----------------------------------------------------

    @staticmethod
    def make_soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    @classmethod
    def select_first(cls, root: Tag, selectors: List[str] | tuple) -> Optional[Tag]:
        for sel in selectors:
            try:
                found = root.select_one(sel)
            except Exception:
                found = None
            if found is not None:
                return found
        return None

    @classmethod
    def select_all_any(cls, root: Tag, selectors: List[str] | tuple) -> List[Tag]:
        """Tous les elements du premier selecteur qui renvoie un resultat."""
        for sel in selectors:
            try:
                found = root.select(sel)
            except Exception:
                found = []
            if found:
                return found
        return []

    @classmethod
    def text_of(cls, el: Optional[Tag]) -> str:
        """Texte propre : blocs `pre` en fenced code, br -> newline, sans UI.

        bs4: `copy.copy(tag)` est une copie profonde -> l'original reste intact
        (on decompose des noeuds pendant la lecture de la structure).
        """
        if el is None:
            return ""
        node = copy.copy(el)
        cls._clean_tree(node)
        return cls._normalize_text(node.get_text())

    @classmethod
    def _clean_tree(cls, node: Tag) -> None:
        for tag in node.find_all(DROP_TAGS):
            tag.decompose()
        for sel in ACTION_SELECTORS:
            for tag in node.select(sel):
                tag.decompose()
        for pre in node.find_all("pre"):
            code = pre.find("code")
            lang = ""
            if code is not None:
                classes = code.get("class") or []
                lang = next(
                    (c.split("-", 1)[1] for c in classes if c.startswith("language-")),
                    "",
                )
            code_text = (code or pre).get_text().rstrip()
            pre.replace_with(NavigableString(f"\n```{lang}\n{code_text}\n```\n"))
        for br in node.find_all("br"):
            br.replace_with(NavigableString("\n"))
        for block in node.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
            block.insert_before(NavigableString("\n"))
            block.insert_after(NavigableString("\n"))

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\u00a0", " ")
        text = ZERO_WIDTH_RE.sub("", text)
        lines = [SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
        text = "\n".join(lines)
        text = NEWLINES_RE.sub("\n\n", text)
        return text.strip()

    @staticmethod
    def extract_title(soup: BeautifulSoup, *selectors: str) -> Optional[str]:
        for sel in selectors:
            try:
                el = soup.select_one(sel)
            except Exception:
                el = None
            if el is not None and el.get_text(strip=True):
                return el.get_text(strip=True)
        og = soup.find("meta", property="og:title") or soup.find(
            "meta", attrs={"name": "title"}
        )
        if og and og.get("content"):
            return str(og["content"]).strip()
        if soup.title and soup.title.get_text(strip=True):
            return soup.title.get_text(strip=True)
        return None

    @classmethod
    def _link_title(cls, a: Tag) -> Optional[str]:
        for attr in ("title", "aria-label"):
            value = a.get(attr)
            if value and str(value).strip():
                return str(value).strip()
        text = a.get_text(" ", strip=True)
        return text or None

    @classmethod
    def _link_section(cls, a: Tag) -> Optional[str]:
        """Date-groupe le plus proche au-dessus du lien (Today/Yesterday...)."""
        current = a
        for _ in range(4):
            parent = current.parent
            if parent is None:
                break
            heading = parent.find_previous(["h2", "h3", "h4", "summary"])
            if heading is not None:
                label = heading.get_text(" ", strip=True)
                if label and len(label) < 40:
                    return label
            current = parent
        return None

    def log_parse(self, conv: Conversation, **fields: Any) -> None:
        log_fields(
            log,
            20,
            f"parsed {self.service_name} conversation",
            extra={
                "conversation_id": conv.conversation_id,
                "messages": len(conv.messages),
                **fields,
            },
        )

    @staticmethod
    def check(conv: Conversation) -> Conversation:
        """Valide et remonte une erreur exploitable pour les fixtures/services."""
        try:
            conv.derive_timestamps()
            conv.validate()
        except SchemaError as exc:
            raise ParseError(str(exc)) from exc
        return conv

    @staticmethod
    def msg(role: str, content: str, timestamp: Any = None, metadata: Optional[Dict] = None) -> Message:
        return Message(role=role, content=content, timestamp=timestamp, metadata=metadata or {})
