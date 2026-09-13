"""Parser DOM Perplexity (perplexity.ai).

Structure reelle observee (2024-2026) :
  - sidebar : historique <a href="/search/<slug>-<id>">
  - question user : <div data-testid="user-query-text">
  - reponse assistant : <div data-testid="answer-text"> (ou [data-testid="answer"])
  - sources : pills de domaine .source-pill / [data-testid*='source'] autour de la reponse
  - horodatage : <time datetime> present dans l'en-tete de thread
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from bs4 import NavigableString, Tag

from ..schema import Conversation
from .base import BaseParser, ParseError, clean_ui_title

CONV_ID_RE = re.compile(r"/search/([A-Za-z0-9_-]{10,})")
MODEL_RE = re.compile(
    r"(sonar[\w.-]*|Perplexity[\w .-]*(?:Pro|Deep Research|Search)?[\w.-]*)",
    re.IGNORECASE,
)


class PerplexityParser(BaseParser):
    service_name = "perplexity"

    link_selectors = (
        "aside a[href*='/search/']",
        "nav a[href*='/search/']",
        "[data-testid='history'] a[href*='/search/']",
        "a[href*='/search/']",
    )
    conversation_id_pattern = CONV_ID_RE

    message_selectors = (
        "div[class~='group/user-bubble']",
        "div[data-workflow-final-text]",
        "div[class~='group/final-text']",
        "[data-testid='user-query-text']",
        "[data-testid='answer-text']",
        "[data-testid='answer']",
    )

    #: 2026: bulles Tailwind sans data-testid (user-bubble / final-text).
    #: `class~=` matche le jeton exact `group/user-bubble` ; l'ancien
    #: `class*=user-bubble` attrapait aussi la barre d'outils du message
    #: (jetons `group-hover/user-bubble:...`), d'ou des horodatages parasites.
    USER_SELECTORS = (
        "div[class~='group/user-bubble']",
        "[data-testid='user-query-text']",
        "[data-testid='user-query'] .query",
        "div.user-query",
    )
    #: `data-workflow-final-text` isole le tour de reponse du header de workflow
    #: (« Recherche terminee ») et du footer d'actions.
    ASSISTANT_SELECTORS = (
        "div[data-workflow-final-text]",
        "div[class~='group/final-text']",
        "[data-testid='answer-text']",
        "[data-testid='answer-body']",
        "[data-testid='answer'] .answer",
        "div.answer",
    )
    SOURCE_SELECTORS = (
        "[data-testid*='source'] a[href^='http']",
        "a.source-pill",
        "[data-testid='citation-source'] a",
        ".source-name",
        "span.citation",  # 2026: citations inline "domaine+1"
    )
    TITLE_SELECTORS = (
        "h1[data-testid='user-query']",
        "header h1",
        "h1",
    )

    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        extra = extra or {}
        soup = self.make_soup(html)

        user_nodes = self.select_all_any(soup, self.USER_SELECTORS)
        assistant_nodes = self.select_all_any(soup, self.ASSISTANT_SELECTORS)

        positions = {}
        for el in soup.descendants:
            positions.setdefault(id(el), len(positions))
        pairs = [(n, "user") for n in user_nodes] + [(n, "assistant") for n in assistant_nodes]
        pairs.sort(key=lambda p: positions.get(id(p[0]), 1 << 30))

        page_timestamp = self._page_timestamp(soup)
        messages: List[Any] = []
        model: Optional[str] = extra.get("model")
        last_user_ts = page_timestamp

        for node, role in pairs:
            content = (
                self._user_text(node) if role == "user"
                else self._assistant_text(node)
            )
            if not content:
                continue
            metadata: Dict[str, Any] = {}
            if role == "user":
                timestamp = last_user_ts
            else:
                metadata["tokens"] = None
                sources = self._sources_of(node)
                if sources:
                    metadata["sources"] = sources
                if not model:
                    model = self._model_of(soup)
                timestamp = page_timestamp
            messages.append(self.msg(role, content, timestamp, metadata))

        if not messages:
            raise ParseError(
                "perplexity: aucun message extrait — session invalide ou DOM modifie"
            )

        title = self.extract_title(soup, *self.TITLE_SELECTORS)
        title = clean_ui_title(title) or clean_ui_title(extra.get("title_hint"))
        if title and len(title) > 120:
            title = None

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = CONV_ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        if not title:
            # repli : premier message user tronque (mieux que le nom du service)
            first_user = next(
                (m.texte for m in messages if m.role == "user"), None
            )
            title = (first_user or "").strip().splitlines()[0][:80] if first_user else None

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Perplexity conversation",
            messages=messages,
            started_at=page_timestamp,
            model=model or None,
        )
        self.log_parse(conv, model=model, title=title)
        return self.check(conv)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _page_timestamp(soup) -> Optional[str]:
        time_el = soup.find("time")
        if time_el is not None:
            return time_el.get("datetime") or time_el.get_text(strip=True)
        return None

    @classmethod
    def _user_text(cls, node: Tag) -> str:
        """Texte de la requete, sans la barre d'outils (horodatage/boutons).

        L'horodatage est dans un conteneur invisible hors survol (classe
        `opacity-0`) ; `text_of` n'ecarte que les `button`/`svg`. Les URLs
        saisies sont rendues en `span[role=button][title=url]` et seraient
        supprimees comme des boutons : on les remet en texte avant nettoyage.
        """
        work = copy.copy(node)
        for toolbar in work.select("[class*='opacity-0']"):
            toolbar.decompose()
        for button in work.select("[role='button'][title]"):
            url = (button.get("title") or "").strip()
            if url.startswith(("http://", "https://")):
                button.replace_with(NavigableString(url))
        return cls.text_of(work)

    @classmethod
    def _assistant_text(cls, node: Tag) -> str:
        """Corps de la reponse, sans l'en-tete de workflow (« Recherche terminee »).

        Le tour `final-text` encapsule un en-tete d'etape, le corps rendu
        (`[data-renderer='lm']`) et un footer d'actions. On ne garde que le(s)
        corps ; un separateur horizontal (`<hr>`) est restaure en `---`.
        """
        bodies = node.select("[data-renderer='lm']")
        if bodies:
            parts: List[str] = []
            for body in bodies:
                work = copy.copy(body)
                for hr in work.find_all("hr"):
                    hr.replace_with(NavigableString("\n\n---\n\n"))
                text = cls.text_of(work)
                if text:
                    parts.append(text)
            if parts:
                return "\n\n".join(parts)
        # repli : retirer l'en-tete de workflow (« Recherche terminee ») et le
        # footer d'actions avant extraction.
        work = copy.copy(node)
        for header in work.select("[class*='step-header']"):
            header.decompose()
        for footer in work.select("[data-workflow-text-footer]"):
            footer.decompose()
        return cls.text_of(work)

    @classmethod
    def _sources_of(cls, answer_node) -> List[str]:
        root = answer_node.parent if answer_node.parent is not None else answer_node
        sources: List[str] = []
        for sel in cls.SOURCE_SELECTORS:
            try:
                found = root.select(sel)
            except Exception:
                found = []
            for el in found:
                href = el.get("href") if el.name == "a" else None
                text = el.get_text(" ", strip=True)
                domain = None
                if href:
                    m = re.search(r"https?://([^/]+)", href)
                    domain = m.group(1) if m else None
                domain = domain or (text.split(" ")[0] if text else None)
                if domain and domain not in sources:
                    sources.append(domain)
        return sources[:25]

    @staticmethod
    def _model_of(soup) -> Optional[str]:
        el = soup.select_one(
            "[data-testid='answer-model-name'], [data-testid='model-name'], footer .text-xs"
        )
        if el is not None:
            text = el.get_text(" ", strip=True)
            m = MODEL_RE.search(text)
            if m:
                return m.group(1).strip()
        return None
