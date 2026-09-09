"""Parser DOM Perplexity (perplexity.ai).

Structure reelle observee (2024-2026) :
  - sidebar : historique <a href="/search/<slug>-<id>">
  - question user : <div data-testid="user-query-text">
  - reponse assistant : <div data-testid="answer-text"> (ou [data-testid="answer"])
  - sources : pills de domaine .source-pill / [data-testid*='source'] autour de la reponse
  - horodatage : <time datetime> present dans l'en-tete de thread
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

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
        "[data-testid='user-query-text']",
        "[data-testid='answer-text']",
        "[data-testid='answer']",
    )

    USER_SELECTORS = (
        "[data-testid='user-query-text']",
        "[data-testid='user-query'] .query",
        "div.user-query",
    )
    ASSISTANT_SELECTORS = (
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
            content = self.text_of(node)
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

        title = self.extract_title(soup, *self.TITLE_SELECTORS) or extra.get("title_hint")
        if title and len(title) > 120:
            title = None

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = CONV_ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        conv = Conversation(
            service=self.service_name,
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
