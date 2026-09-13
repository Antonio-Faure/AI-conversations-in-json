"""Parser DOM ChatGPT (chatgpt.com).

Structure reelle observee (2024-2026):
  - sidebar : <nav aria-label="Chat history"> avec <a href="/c/<uuid>"> par conversation
  - messages : <div data-message-id="<uuid>" data-message-author-role="user|assistant"
               data-message-model-slug="gpt-4o"> <div class="markdown ...">...</div>
  - timestamps : absents du DOM visible -> injectes par le service via les donnees
    internes React (`memoizedProps.message.create_time`), passees en `extra`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

CONVERSATION_ID_RE = re.compile(r"/c/([0-9a-fA-F-]{16,}|[A-Za-z0-9_-]{16,})")


def merge_turn_snapshots(
    snapshots: List[List[str]], html_by_key: Dict[str, str]
) -> List[str]:
    """Reconstruit l'ordre des tours a partir des fenetres vues au scroll.

    ChatGPT virtualise les fils longs : a chaque instant le DOM ne contient
    qu'une fenetre de tours. En scrollant, on memorise par cle (message-id ou
    contenu) chaque fenetre ordonnee. On assemble ensuite les aretes de
    succession ``cle -> cle suivante`` en une chaine, sans doublon : la
    chronologie est independante du sens du scroll.

    Les composants disjoints (tours non relies, ex. scroll trop rapide) sont
    ordonnes par ordre de decouverte inverse : en remontant le fil, un
    composant decouvert plus tard est plus ancien.

    Retourne les fragments HTML dans l'ordre chronologique.
    """
    successor: Dict[str, str] = {}
    discovery: Dict[str, int] = {}
    for index, snapshot in enumerate(snapshots):
        for key in snapshot:
            discovery.setdefault(key, index)
        for left, right in zip(snapshot, snapshot[1:]):
            successor.setdefault(left, right)
    predecessor = {right: left for left, right in successor.items()}

    components: List[tuple] = []
    remaining = set(discovery)
    while remaining:
        starts = sorted(
            (key for key in remaining if key not in predecessor),
            key=lambda key: discovery[key],
        )
        start = starts[0] if starts else min(remaining, key=lambda key: discovery[key])
        component: List[str] = []
        current: Optional[str] = start
        while current is not None and current in remaining:
            component.append(current)
            remaining.discard(current)
            current = successor.get(current)
        components.append((discovery[start], component))

    components.sort(key=lambda item: item[0], reverse=True)
    ordered = [key for _, component in components for key in component]
    return [html_by_key[key] for key in ordered if key in html_by_key]


_MSG_ID_ATTR_RE = re.compile(r"data-message-id=['\"]([^'\"]+)['\"]")


def _fragment_message_id(fragment: str) -> Optional[str]:
    match = _MSG_ID_ATTR_RE.search(fragment)
    return match.group(1) if match else None


def order_fragments_by_time(
    fragments: List[str], message_meta: Optional[Dict[str, Dict[str, Any]]]
) -> List[str]:
    """Reordonne les fragments par timestamp React (`create_time`).

    Le scroll par fenetres peut laisser des trous dans la chaine de succession
    (virtualisation ChatGPT) : les timestamps donnent une chronologie fiable.
    Les fragments sans timestamp (tours image sans message-id) sont remplis par
    voisinage avant/arriere, puis le tri stable preserve l'ordre a egalite.
    """
    if not fragments or not message_meta:
        return fragments
    times: List[Optional[float]] = []
    for fragment in fragments:
        mid = _fragment_message_id(fragment)
        entry = message_meta.get(mid) if mid else None
        value = entry.get("time") if isinstance(entry, dict) else None
        times.append(float(value) if isinstance(value, (int, float)) else None)

    previous: Optional[float] = None
    for index, value in enumerate(times):
        if value is None:
            times[index] = (previous + 1e-6) if previous is not None else None
        else:
            previous = value
    following: Optional[float] = None
    for index in range(len(times) - 1, -1, -1):
        value = times[index]
        if value is None:
            times[index] = (
                following - 1e-6 if following is not None else float(index)
            )
        else:
            following = value

    order = sorted(range(len(fragments)), key=lambda index: times[index])
    return [fragments[index] for index in order]


class ChatGPTParser(BaseParser):
    service_name = "chatgpt"

    link_selectors = (
        "nav[aria-label='Chat history'] a[href*='/c/']",
        "aside nav a[href*='/c/']",
        "[data-testid='sidebar'] a[href*='/c/']",
        "main nav a[href*='/c/']",
        "a[href^='/c/']",
    )
    conversation_id_pattern = CONVERSATION_ID_RE

    message_selectors = (
        "div[data-message-author-role='assistant']",
        "div[data-message-id]",
        "[data-testid^='conversation-turn']",
    )

    TURN_SELECTORS = (
        "[data-message-author-role][data-message-id]",
        "[data-message-author-role]",
        "div[data-message-id]",
        "[data-testid^='conversation-turn']",
        "main article[role='article']",
    )
    CONTENT_SELECTORS = (
        "[data-testid^='conversation-turn-text']",
        ".markdown",
        "div.whitespace-pre-wrap",
        ".prose",
        "p",
    )
    TITLE_SELECTORS = (
        "[data-testid='history-title']",
        "header [class*='truncate']",
        "header h1",
    )
    MODEL_HEADER_SELECTORS = (
        "[data-testid='model-switcher-dropdown-button']",
        "header button[aria-haspopup]",
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
        message_meta: Dict[str, Dict[str, Any]] = extra.get("messages") or {}

        turns = self._collect_turns(soup)
        messages: List[Any] = []
        models: List[str] = []

        for turn, role in turns:
            content_el = self.select_first(turn, self.CONTENT_SELECTORS)
            content = self.text_of(content_el)
            # pieces jointes / images generees (souvent hors du .markdown)
            attachments = self.attachments_markdown(turn, url_filter=self._is_content_image)
            if attachments and attachments not in content:
                content = f"{content}\n\n{attachments}".strip() if content else attachments
            if not content:
                continue
            mid = turn.get("data-message-id")
            meta: Dict[str, Any] = {}
            timestamp = None
            slug = turn.get("data-message-model-slug")
            if mid and mid in message_meta:
                mm = message_meta[mid]
                timestamp = mm.get("time")
                slug = mm.get("model") or slug
            if role == "assistant":
                meta["tokens"] = None
                if slug:
                    models.append(slug)
            messages.append(
                self.msg(
                    role,
                    content,
                    timestamp,
                    meta,
                    message_id=str(mid or ""),
                    model=slug,
                )
            )

        # le <title> du DOM reste "ChatGPT" tant que la page n'a pas hydrate :
        # le titre de la sidebar (title_hint, via ref) est fiable, on le
        # privilegie ; le titre DOM n'est garde que s'il est specifique.
        dom_title = self.extract_title(soup, *self.TITLE_SELECTORS)
        generic = {"chatgpt", "new chat", "nouvelle conversation", ""}
        if dom_title and dom_title.strip().lower() in generic:
            dom_title = None
        title = extra.get("title_hint") or dom_title

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = CONVERSATION_ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        model = extra.get("model") or self.majority(models)
        if not model:
            header = self.select_first(soup, self.MODEL_HEADER_SELECTORS)
            text = self.text_of(header)
            if text and len(text) < 60:
                model = text

        if not messages:
            raise ParseError(
                "chatgpt: aucun message extrait — session invalide ou DOM modifie"
            )

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "ChatGPT conversation",
            messages=messages,
            started_at=extra.get("created_at"),
            model=model,
        )
        self.log_parse(conv, model=model, title=title)
        return self.check(conv)

    @staticmethod
    def _is_content_image(src: str) -> bool:
        return ("estuary" in src) or ("oaiusercontent" in src)

    def _is_image_turn(self, turn) -> bool:
        """Tour assistant sans role explicite mais portant une image generee."""
        if turn.select_one("[data-testid='image-gen-overlay-actions']") is not None:
            return True
        for img in turn.find_all("img"):
            if (img.get("aria-hidden") or "").lower() == "true":
                continue
            if (img.get("alt") or "").startswith("Generated image"):
                return True
            if self._is_content_image(img.get("src") or ""):
                return True
        return False

    def _collect_turns(self, soup):
        """Tours dans l'ordre du document, images generees incluses.

        ChatGPT rend parfois un tour image (assistant) sans
        `data-message-author-role` : il faut le conserver pour ne pas fusionner
        deux messages utilisateur consecutifs.
        """
        positions: Dict[Any, int] = {}
        for element in soup.descendants:
            positions.setdefault(id(element), len(positions))
        turns = []
        for el in soup.select("[data-message-author-role]"):
            if el.get("style") == "display: none;":
                continue
            turns.append((positions.get(id(el), 0), el, self._role_of(el)))
        for el in soup.select("[data-testid^='conversation-turn']"):
            if el.select_one("[data-message-author-role]") is not None:
                continue
            if self._is_image_turn(el):
                turns.append((positions.get(id(el), 0), el, "assistant"))
        turns.sort(key=lambda item: item[0])
        return [(turn, role) for _, turn, role in turns]

    @staticmethod
    def _role_of(turn) -> str:
        attr = (turn.get("data-message-author-role") or "").lower()
        if attr in ("user", "assistant", "system", "tool"):
            return attr
        testid = (turn.get("data-testid") or "").lower()
        if "assistant" in testid:
            return "assistant"
        classes = " ".join(turn.get("class") or []).lower()
        if "mention-sticker" in classes or "bg-surface-2xs" in classes:
            return "user"
        return "assistant"
