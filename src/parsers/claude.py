"""Parser DOM Claude (claude.ai).

Deux generations de DOM coexistent selon les conversations :
  - classique : messages user <div data-testid="user-message">, reponses
    <div data-test="collapsible-text" class="font-claude-message"> ou
    <div data-testid="assistant-message-text">
  - transcript (2026, structure [data-testid='transcript-row']) : reponses
    assistant dans <div class="font-claude-response"> > .prose > .standard-markdown ;
    les marqueurs assistant classiques y ont disparu (les user restent).
Blocs reflexion [data-testid="thinking-block"] exclus du texte final.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

UUID_RE = re.compile(r"/chat/([0-9a-fA-F]{8}-(?:[0-9a-fA-F-]{27}|[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))")

#: label UI du bloc reflexion replie ("A réfléchi pendant 26 s") — pas du contenu
THINKING_LABEL_RE = re.compile(
    r"^\s*(?:a\s+r[eé]fl[eé]chi\s+(?:\S+\s+)?pendant\s+(?:\d+\s*s(?:ec\.?|econdes?)?|quelques\s+secondes)"
    r"|thought\s+for\s+.*?seconds?)\s*:?\s*$",
    re.IGNORECASE,
)

USER_ATTRS = ("data-testid", "data-test", "data-testid", "data-test")
USER_VALUES = ("user-message", "user-editor", "user-message-content")
ASSISTANT_VALUES = ("assistant-message-text", "collapsible-text")


class ClaudeParser(BaseParser):
    service_name = "claude"

    link_selectors = (
        "[data-testid='sidebar-chats-list'] a[href*='/chat/']",
        "nav a[href*='/chat/']",
        "aside a[href*='/chat/']",
        "a[href*='/chat/']",
    )
    conversation_id_pattern = UUID_RE

    message_selectors = (
        "div[data-testid='user-message']",
        "div.font-claude-response",
        "div[data-testid='assistant-message-text']",
        "div[data-test='collapsible-text']",
        "div[data-test='user-message']",
    )

    #: selecteur combine des deux roles (ordre de document preserve par bs4)
    TURN_SELECTOR = ", ".join(
        [f"div[{a}='{v}']" for a, v in zip(USER_ATTRS, USER_VALUES)]
        + [f"div[{a}='{v}']" for a in ("data-testid", "data-test") for v in ASSISTANT_VALUES]
        + ["div.font-claude-message", "div.font-claude-response"]
    )
    THINKING_SELECTORS = (
        "[data-testid='thinking-block']",
        "[data-test='thinking-toggle']",
        # statut de tour 2026 : label de reflexion + resume + statuts outils
        "[data-cds='TurnStatus']",
        "[data-testid='TurnStatus']",
        "[role='status']",
        "details",
    )
    CONTENT_SELECTORS = (".font-claude-message", "div", "p")
    TITLE_SELECTORS = (
        "[data-testid='chat-header-title']",
        "header h1",
        "h1[class*='truncate']",
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

        try:
            turns = soup.select(self.TURN_SELECTOR)
        except Exception:
            turns = []
        # un element peut matcher user et assistant : priorite a l'attribut role
        seen_ids = set()
        deduped = []
        for t in turns:
            key = (id(t), t.get("data-testid"), t.get("data-test"))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            deduped.append(t)
        # retirer les noeuds englobants redondants (ex: font-claude-message
        # qui contient deja un collapsible-text) en comparant par identite
        deduped = [
            t
            for t in deduped
            if not any(o is not t and any(d is t for d in o.descendants) for o in deduped)
        ]
        turns = deduped

        messages: List[Any] = []
        for turn in turns:
            role = self._role_of(turn)
            node = self._strip_thinking(turn)
            content = self.text_of(node)
            content = self._drop_thinking_labels(content)
            if not content:
                continue
            timestamp = self._timestamp_of(turn)
            metadata: Dict[str, Any] = {}
            if role == "assistant":
                metadata["tokens"] = None
                if self._was_thinking(turn):
                    metadata["had_thinking"] = True
            messages.append(self.msg(role, content, timestamp, metadata))

        if not messages:
            raise ParseError(
                "claude: aucun message extrait — session invalide ou DOM modifie"
            )

        title = self.extract_title(soup, *self.TITLE_SELECTORS) or extra.get("title_hint")
        if title:
            title = re.sub(r"\s+[—-]\s*Claude\s*$", "", title)

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = UUID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        model = None
        badge = soup.select_one("[data-testid='model-badge']")
        if badge is not None:
            model = self.text_of(badge)
        if not model:
            selector = soup.select_one("[data-testid='model-selector-dropdown']")
            if selector is not None:
                label = selector.get("aria-label") or ""
                model = label.split(":", 1)[-1].strip() if ":" in label else label.strip()
        if model and len(model) > 60:
            model = None

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Claude conversation",
            messages=messages,
            model=model or None,
        )
        self.log_parse(conv, model=model, title=title)
        return self.check(conv)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _drop_thinking_labels(content: str) -> str:
        """Retire les labels UI 'A réfléchi pendant N s' du texte extrait."""
        if not content:
            return content
        kept = [l for l in content.splitlines() if not THINKING_LABEL_RE.match(l)]
        cleaned = "\n".join(kept)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    @staticmethod
    def _role_of(turn) -> str:
        attrs = (turn.get("data-testid") or "", turn.get("data-test") or "")
        if any(v in attrs for v in USER_VALUES):
            return "user"
        if any(v in attrs for v in ASSISTANT_VALUES):
            return "assistant"
        return "assistant"

    @classmethod
    def _strip_thinking(cls, turn):
        import copy

        node = copy.copy(turn)
        for sel in cls.THINKING_SELECTORS:
            for el in node.select(sel):
                el.decompose()
        return node

    @staticmethod
    def _was_thinking(turn) -> bool:
        if turn.select_one("[data-testid='thinking-block']"):
            return True
        # le bloc reflexion est souvent frere du texte, dans la meme rangee
        parent = turn.parent
        if parent is not None and len(parent.select(ClaudeParser.TURN_SELECTOR)) <= 2:
            return bool(parent.select_one("[data-testid='thinking-block']"))
        return False

    @classmethod
    def _timestamp_of(cls, turn):
        scopes = [turn]
        parent = turn.parent
        # le parent n'est probe que s'il s'agit d'une rangee de message
        # individuelle (pas le conteneur de tout le fil, sinon chaque tour
        # heriterait du premier <time> de la page)
        if parent is not None and len(parent.select(cls.TURN_SELECTOR)) <= 1:
            scopes.append(parent)
        for scope in scopes:
            time_el = scope.find("time")
            if time_el is not None and time_el.get("datetime"):
                return time_el["datetime"]
        return None
