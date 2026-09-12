"""Parser DOM Gemini (gemini.google.com/app).

Structure reelle observee (2024-2026, app Angular) :
  - sidebar : element <conversation-history> avec <a href="/app/<id>">
  - message user : <user-query> (contenu dans .user-query-content .content,
    ou <message-content> imbrique, ou rich-textarea .ql-editor)
  - reponse assistant : <model-response> <message-content class="model-response-text">
  - timestamps absents du DOM (derive de l'ordre des messages)
  - modele : label de reponse [data-test-id='response-model-label'] ou texte
    "Answered by Gemini 2.x" dans le pied de reponse.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, NavigableString

from ..schema import Conversation
from .base import BaseParser, ParseError, clean_ui_title

# IDs Gemini observes : 16 caracteres hexa (/app/892fb59332022e2a)
CONV_ID_RE = re.compile(r"/app/([a-zA-Z0-9_-]{8,})")
MODEL_TEXT_RE = re.compile(
    r"(Answered by\s+|Model:?\s+|)(Gemini(?:\s+Ultra|\s+\d[\d.]*\s*\w*)?(?:\s+(?:Pro|Flash|Live|Thinking|Deep Research))?)",
    re.IGNORECASE,
)

#: conteneur d'un tour complet (requete + reponse) dans le DOM Gemini
TURN_SELECTOR = ".conversation-container"
#: selecteur de la reponse textuelle d'un tour
TURN_ANSWER_SELECTOR = (
    "model-response message-content .model-response-text, "
    "model-response message-content, message-content"
)


def _turn_user_key(turn_soup: BeautifulSoup) -> str:
    """Clef d'identite d'un tour : requete utilisateur normalisee (+ images).

    L'id du conteneur change d'un rendu virtualise a l'autre ; on s'appuie
    donc sur le contenu de la requete pour reconnaitre un meme tour.
    """
    user = turn_soup.select_one("user-query")
    if user is None:
        return ""
    text = re.sub(r"\s+", " ", user.get_text(" ", strip=True)).strip().lower()
    images = " ".join(
        (img.get("src") or img.get("alt") or "") for img in user.find_all("img")
    )
    return f"{text}|{images}"


def _turn_has_answer(turn_soup: BeautifulSoup) -> bool:
    answer = turn_soup.select_one(TURN_ANSWER_SELECTOR)
    return bool(answer is not None and answer.get_text(strip=True))


def merge_turn_fragments(fragments: List[str]) -> str:
    """Fusionne des tours autonomes en un HTML unique, sans doublon.

    Gemini virtualise le fil : en remontant, le DOM rend parfois plusieurs
    exemplaires d'un meme tour (fenetres re-rendues, variantes de reponse).
    On garde, pour chaque requete utilisateur, l'occurrence la plus complete
    (reponse non vide) et la plus tardive dans l'ordre du document — celle du
    fil principal.

    Retourne un document HTML minimal parsable par :meth:`GeminiParser.parse`.
    """
    parsed: List[tuple] = []
    for fragment in fragments:
        soup = BeautifulSoup(fragment, "html.parser")
        user = soup.select_one("user-query")
        if user is None:
            continue
        parsed.append((_turn_user_key(soup), _turn_has_answer(soup), fragment))
    if not parsed:
        return ""

    best: Dict[str, tuple] = {}
    for index, (key, has_answer, fragment) in enumerate(parsed):
        previous = best.get(key)
        # >= : a score egal, l'occurrence la plus tardive prime (fil principal)
        if previous is None or has_answer >= previous[0]:
            best[key] = (has_answer, index, fragment)
    selected = [entry[2] for entry in sorted(best.values(), key=lambda e: e[1])]
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        + "".join(selected)
        + "</body></html>"
    )


class GeminiParser(BaseParser):
    service_name = "gemini"

    link_selectors = (
        "conversation-history a[href*='/app/']",
        "mat-nav-list a[href*='/app/']",
        "aside a[href*='/app/']",
        "a[href*='gemini.google.com/app/']",
        "a[href*='/app/']",
    )
    conversation_id_pattern = CONV_ID_RE

    message_selectors = (
        "user-query",
        "model-response",
        "message-content",
    )

    USER_SELECTORS = (
        "user-query .user-query-content .content",
        "user-query .content",
        "user-query message-content .model-response-text",
        "user-query message-content",
        "user-query rich-textarea .ql-editor",
        "user-query",
    )
    ASSISTANT_SELECTORS = (
        "model-response message-content .model-response-text",
        "model-response message-content",
        "message-content.model-response-text",
        "model-response",
    )
    TITLE_SELECTORS = (
        "[data-test-id='conversation-title']",
        "chat-header .glimpse-title",
        "chat-title",
        "header h1",
    )
    FOOTER_TEXT_SELECTORS = (
        "[data-test-id='response-footer']",
        ".response-footer",
        "message-actions",
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

        user_nodes = self.select_all_any(soup, self.USER_SELECTORS[:6])
        assistant_nodes = self.select_all_any(soup, self.ASSISTANT_SELECTORS[:4])

        pairs = [(n, "user") for n in user_nodes] + [(n, "assistant") for n in assistant_nodes]
        # ordre du document
        positions = {}
        for el in soup.descendants:
            positions.setdefault(id(el), len(positions))
        pairs.sort(key=lambda p: positions.get(id(p[0]), 1 << 30))

        messages: List[Any] = []
        model: Optional[str] = extra.get("model")
        for node, role in pairs:
            if role == "user":
                content_el = self.select_first(node, self.CONTENT_OF_USER)
            else:
                content_el = self.select_first(node, self.CONTENT_OF_ASSISTANT)
            content = self._content_text(content_el if content_el is not None else node)
            if role == "user":
                attach_root = node.find_parent("user-query") or node
                attachments = self.attachments_markdown(
                    attach_root, url_filter=self._is_content_image
                )
                if attachments and attachments not in content:
                    content = f"{content}\n\n{attachments}".strip() if content else attachments
            if not content:
                continue
            metadata: Dict[str, Any] = {}
            if role == "assistant":
                metadata["tokens"] = None
                if not model:
                    # le pied de reponse est un frere du noeud de texte :
                    # on sonde l'ancetre direct, puis la page entiere
                    model = self._model_from_footer(node.parent) or self._model_from_footer(soup)
            messages.append(self.msg(role, content, None, metadata))

        if not messages:
            raise ParseError(
                "gemini: aucun message extrait — session invalide ou DOM modifie"
            )

        title = self.extract_title(soup, *self.TITLE_SELECTORS) or extra.get("title_hint")
        title = clean_ui_title(title) or clean_ui_title(extra.get("title_hint"))
        if title:
            title = re.sub(r"\s+[—-]\s*Gemini\s*$", "", title)

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = CONV_ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Gemini conversation",
            messages=messages,
            model=model or None,
        )
        self.log_parse(conv, model=model, title=title)
        return self.check(conv)

    CONTENT_OF_USER = (
        # parent de tous les .query-text-line (requete multi-ligne)
        "user-query .query-text",
        ".user-query-content .content",
        ".content",
        "rich-textarea .ql-editor",
        "message-content",
    )
    CONTENT_OF_ASSISTANT = (
        "message-content .model-response-text",
        ".model-response-text",
        "message-content",
    )

    @classmethod
    def _content_text(cls, el) -> str:
        """Texte markdown d'un contenu Gemini (listes et tableaux preserves).

        ``BaseParser.text_of`` aplatit les ``<ul>/<ol>`` et les ``<table>`` :
        on les convertit en markdown sur une copie avant l'extraction. Les
        conversions sont reinjectees apres le nettoyage (qui supprime
        l'indentation des lignes) pour conserver les listes imbriquees.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        replacements: Dict[str, str] = {}

        for index, table in enumerate(node.find_all("table")):
            token = f"@@AICV_TABLE_{index}@@"
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(token))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = f"@@AICV_LIST_{index}@@"
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(token))

        text = cls.text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text

    @classmethod
    def _table_markdown(cls, table) -> str:
        """Tableau HTML -> lignes markdown ``| ... |`` avec separateur."""
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            rows.append([c.get_text(" ", strip=True) for c in cells])
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        lines = []
        for index, row in enumerate(rows):
            padded = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(padded) + " |")
            if index == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        return "\n".join(lines)

    @classmethod
    def _render_list(cls, lst, depth: int = 0) -> List[str]:
        ordered = lst.name == "ol"
        try:
            start = int(lst.get("start") or 1)
        except (TypeError, ValueError):
            start = 1
        lines: List[str] = []
        for offset, li in enumerate(lst.find_all("li", recursive=False)):
            li_copy = copy.copy(li)
            for sub in li_copy.find_all(["ul", "ol"]):
                sub.decompose()
            text = re.sub(r"\s*\n\s*", " ", cls.text_of(li_copy)).strip()
            marker = f"{start + offset}." if ordered else "-"
            lines.append(f"{'  ' * depth}{marker} {text}")
            for sub in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(cls._render_list(sub, depth + 1))
        return lines

    @staticmethod
    def _is_content_image(src: str) -> bool:
        """Image de contenu Gemini (piece jointe ou image generee)."""
        return "googleusercontent.com/gg" in src

    def _model_from_footer(self, assistant_node) -> Optional[str]:
        for sel in self.FOOTER_TEXT_SELECTORS:
            try:
                footers = assistant_node.select(sel)
            except Exception:
                footers = []
            for footer in footers:
                text = footer.get_text(" ", strip=True)
                m = MODEL_TEXT_RE.search(text)
                if m:
                    name = m.group(2) or m.group(0)
                    return re.sub(r"\s+", " ", name).strip()
        return None
