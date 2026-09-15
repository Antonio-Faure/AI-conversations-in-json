"""Parser DOM ChatGPT (chatgpt.com).

Structure reelle observee (2024-2026):
  - sidebar : <nav aria-label="Chat history"> avec <a href="/c/<uuid>"> par conversation
  - messages : <div data-message-id="<uuid>" data-message-author-role="user|assistant"
               data-message-model-slug="gpt-4o"> <div class="markdown ...">...</div>
  - timestamps : absents du DOM visible -> injectes par le service via les donnees
    internes React (`memoizedProps.message.create_time`), passees en `extra`.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from bs4 import NavigableString

from ..schema import Conversation
from .base import BaseParser, ParseError

CONVERSATION_ID_RE = re.compile(r"/c/([0-9a-fA-F-]{16,}|[A-Za-z0-9_-]{16,})")

#: domaines des visuels de contenu ChatGPT (pieces jointes + images generees)
CONTENT_IMAGE_HOSTS = ("estuary", "oaiusercontent", "images.openai.com")

#: langage affiche par ChatGPT -> identifiant usuel de fence markdown
_LANGUAGE_ALIASES = {
    "c++": "cpp",
    "c#": "csharp",
    "objective-c": "objectivec",
    "shell": "bash",
    "plain text": "text",
}


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
            content = self._content_text(content_el)
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
        return any(host in src for host in CONTENT_IMAGE_HOSTS)

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
        `data-message-author-role` : le role est alors porte par le conteneur
        `[data-testid^='conversation-turn'][data-turn]`. On conserve ces tours
        pour ne pas fusionner deux messages du meme role consecutifs.
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
            if el.get("style") == "display: none;":
                continue
            role = (el.get("data-turn") or "").lower()
            if role not in ("user", "assistant", "system", "tool"):
                if not self._is_image_turn(el):
                    continue
                role = "assistant"
            turns.append((positions.get(id(el), 0), el, role))
        turns.sort(key=lambda item: item[0])
        return [(turn, role) for _, turn, role in turns]

    # -- rendu markdown (titres, listes, tableaux, code, LaTeX) ----------------

    @classmethod
    def _content_text(cls, el) -> str:
        """Texte markdown d'un tour ChatGPT.

        ``BaseParser.text_of`` aplatit les ``<ul>/<ol>``, ``<table>``,
        ``<h1..h6>``, ``<blockquote>``, ``<hr>`` et les mises en forme inline
        (``strong``/``em``/``del``/``code``). On les convertit en markdown sur
        une copie avant l'extraction, puis on reinjecte les remplacements
        apres nettoyage (qui supprime l'indentation) pour conserver listes
        imbriquees et separateurs.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        replacements: Dict[str, str] = {}
        cls._drop_math_block_controls(node)
        cls._latex_to_markdown(node)
        cls._citation_pills(node)
        cls._code_languages(node)
        cls._inline_markup(node)

        # blockquotes : traiter les plus externes (recursion du contenu)
        for index, quote in enumerate(node.find_all("blockquote")):
            if quote.find_parent("blockquote") is not None:
                continue
            inner = cls._content_text(quote)
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in inner.splitlines()
            )
            token = f"@@AICV_CHATGPT_QUOTE_{index}@@"
            replacements[token] = quoted
            quote.replace_with(NavigableString(f"\n{token}\n"))

        for index, table in enumerate(node.find_all("table")):
            token = f"@@AICV_CHATGPT_TABLE_{index}@@"
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = f"@@AICV_CHATGPT_LIST_{index}@@"
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(f"\n{token}\n"))

        for index, heading in enumerate(
            node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ):
            level = int(heading.name[1])
            title = re.sub(r"\s*\n\s*", " ", cls._content_text(heading)).strip()
            token = f"@@AICV_CHATGPT_HEADING_{index}@@"
            replacements[token] = f"{'#' * level} {title}".strip()
            heading.replace_with(NavigableString(f"\n{token}\n"))

        for hr in node.find_all("hr"):
            hr.replace_with(NavigableString("\n@@AICV_CHATGPT_HR@@\n"))

        text = cls.text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text.replace("@@AICV_CHATGPT_HR@@", "---")

    @classmethod
    def _inline_markup(cls, node) -> None:
        """Mises en forme inline -> markdown (hors blocs de code).

        Les elements sont traites du plus profond au plus externe pour que les
        imbrications (``**gras *italique***``) soient preservees.
        """
        tags = node.find_all(["strong", "b", "em", "i", "del", "s", "code"])
        for tag in reversed(tags):
            if tag.find_parent("pre") is not None:
                continue  # contenu de bloc de code : deja gere par text_of
            if tag.name == "code":
                code = tag.get_text()
                if code:
                    tag.replace_with(NavigableString(f"`{code}`"))
                continue
            inner = re.sub(r"\s*\n\s*", " ", cls.text_of(copy.copy(tag))).strip()
            if not inner:
                continue
            if tag.name in ("strong", "b"):
                markdown = f"**{inner}**"
            elif tag.name in ("em", "i"):
                markdown = f"*{inner}*"
            else:
                markdown = f"~~{inner}~~"
            tag.replace_with(NavigableString(markdown))

    @classmethod
    def _latex_to_markdown(cls, node) -> None:
        """KaTeX ChatGPT -> ``$...$`` / ``$$...$$``.

        ChatGPT porte la source TeX dans ``data-math-source`` (qui contient le
        rendu KaTeX) ; ``BaseParser`` la convertit en ``\\(...\\)``. On prefere
        la convention ``$...$`` et on detecte les equations en bloc via
        ``style="display: block"``, la classe de bloc ou le panneau de legende.
        """
        for element in node.select("[data-math-source]"):
            if element.find_parent(attrs={"data-math-source": True}) is not None:
                continue
            tex = (element.get("data-math-source") or "").strip()
            if not tex:
                continue
            element.replace_with(
                NavigableString(
                    f"\n$${tex}$$\n" if cls._math_is_display(element) else f" ${tex}$ "
                )
            )
        for katex in node.select("span.katex"):
            if katex.find_parent(attrs={"data-math-source": True}) is not None:
                continue
            annotation = katex.find(
                "annotation", attrs={"encoding": "application/x-tex"}
            )
            if annotation is None:
                continue
            tex = annotation.get_text().strip()
            if not tex:
                continue
            display = (
                katex.find_parent(class_="katex-display") is not None
                or cls._math_is_display(katex)
            )
            katex.replace_with(
                NavigableString(f"\n$${tex}$$\n" if display else f" ${tex}$ ")
            )

    @staticmethod
    def _math_is_display(element) -> bool:
        style = (element.get("style") or "").replace(" ", "").lower()
        classes = " ".join(element.get("class") or [])
        if "display:block" in style or "mathBlock" in classes:
            return True
        return (
            element.find_parent(class_="katex-display") is not None
            or element.find_parent(class_="caption-text") is not None
        )

    @staticmethod
    def _drop_math_block_controls(node) -> None:
        """Retire les curseurs d'un bloc math interactif (garde les equations).

        Ces cartes embarquent des sliders (``a``/``b``) et leurs libelles KaTeX
        qui n'ont pas de sens en markdown ; le texte ``sr-only`` decrivant
        l'interaction est conserve.
        """
        for card in node.select("[data-testid='math-block-layout']"):
            description = " ".join(
                s.get_text(" ", strip=True) for s in card.select(".sr-only")
            ).strip()
            for panel in card.select(".control-panel"):
                panel.decompose()
            if description:
                card.insert_after(NavigableString(f"\n{description}\n"))

    @classmethod
    def _citation_pills(cls, node) -> None:
        """Pastilles de source web -> lien markdown (sans favicon/etat hover)."""
        for pill in node.select("[data-testid='webpage-citation-pill']"):
            link = pill.find("a")
            if link is None:
                continue
            href = (link.get("href") or "").strip()
            label_el = link.select_one("span.truncate")
            label = (
                label_el.get_text(" ", strip=True)
                if label_el is not None
                else link.get_text(" ", strip=True)
            )
            label = re.sub(r"\s+", " ", label).strip()
            count = ""
            for span in link.find_all("span"):
                text = span.get_text(" ", strip=True)
                if text.startswith("+") and text[1:].isdigit():
                    count = text
                    break
            if count and count not in label:
                label = f"{label} {count}".strip()
            if not href or not label:
                pill.decompose()
                continue
            pill.replace_with(
                NavigableString(f" [{cls._md_escape_label(label)}]({href}) ")
            )

    @classmethod
    def _code_languages(cls, node) -> None:
        """Injecte la langue des blocs de code ChatGPT (en-tete de la carte)."""
        for pre in node.find_all("pre"):
            if pre.get("data-language"):
                continue
            header = pre.select_one("div.font-sans")
            if header is None:
                continue
            label_el = header.find("div", recursive=False)
            if label_el is None:
                continue
            label = re.sub(r"\s+", " ", label_el.get_text(" ", strip=True)).strip()
            if not label or len(label) > 30:
                continue
            pre["data-language"] = cls._normalize_language(label)

    @staticmethod
    def _normalize_language(label: str) -> str:
        language = label.strip().lower()
        return _LANGUAGE_ALIASES.get(language, language)

    @classmethod
    def _inline_text(cls, el) -> str:
        """Texte markdown inline (sans structure de blocs)."""
        if el is None:
            return ""
        return cls._content_text(el)

    @classmethod
    def _table_markdown(cls, table) -> str:
        """Tableau HTML -> lignes markdown ``| ... |`` avec separateur."""
        rows: List[List[str]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            rows.append([cls._inline_text(c).strip() for c in cells])
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        lines: List[str] = []
        for index, row in enumerate(rows):
            padded = row + [""] * (width - len(row))
            lines.append("| " + " | ".join(padded) + " |")
            if index == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        return "\n".join(lines)

    @classmethod
    def _render_list(cls, lst, depth: int = 0) -> List[str]:
        """Liste HTML -> lignes markdown (puces, numeros, cases a cocher)."""
        ordered = lst.name == "ol"
        try:
            start = int(lst.get("start") or 1)
        except (TypeError, ValueError):
            start = 1
        lines: List[str] = []
        for offset, li in enumerate(lst.find_all("li", recursive=False)):
            checkbox = li.find("input", attrs={"type": "checkbox"})
            if checkbox is not None:
                marker = f"- [{'x' if checkbox.has_attr('checked') else ' '}]"
            else:
                marker = f"{start + offset}." if ordered else "-"
            li_copy = copy.copy(li)
            for sub in li_copy.find_all(["ul", "ol"]):
                sub.decompose()
            for box in li_copy.find_all("input"):
                box.decompose()
            text = re.sub(r"\s*\n\s*", " ", cls._content_text(li_copy)).strip()
            lines.append(f"{'  ' * depth}{marker} {text}".rstrip())
            for sub in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(cls._render_list(sub, depth + 1))
        return lines

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
