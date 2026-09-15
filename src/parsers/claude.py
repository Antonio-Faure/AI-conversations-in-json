"""Parser DOM Claude (claude.ai).

Deux generations de DOM coexistent selon les conversations :
  - classique : messages user <div data-testid="user-message">, reponses
    <div data-test="collapsible-text" class="font-claude-message"> ou
    <div data-testid="assistant-message-text">
  - transcript (2026, structure [data-testid='transcript-row']) : reponses
    assistant dans <div class="font-claude-response"> > .prose > .standard-markdown ;
    les marqueurs assistant classiques y ont disparu. Les tours user y sont
    balises par [data-cds='UserMessage'] (et non plus [data-testid='user-message']).
Blocs reflexion [data-testid="thinking-block"] exclus du texte final.

Le texte est rendu en markdown complet (titres, listes, tableaux, citations,
separateurs, gras/italique/barre, code, LaTeX) ; les pieces jointes des tours
user (images, fichiers) sont ajoutees en markdown.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from bs4 import NavigableString, Tag

from ..schema import Conversation
from .base import BaseParser, ParseError

UUID_RE = re.compile(r"/chat/([0-9a-fA-F]{8}-(?:[0-9a-fA-F-]{27}|[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}))")

#: label UI du bloc reflexion replie ("A réfléchi pendant 26 s") — pas du contenu
THINKING_LABEL_RE = re.compile(
    r"^\s*(?:a\s+r[eé]fl[eé]chi\s+(?:\S+\s+)?pendant\s+(?:\d+\s*s(?:ec\.?|econdes?)?|quelques\s+secondes)"
    r"|thought\s+for\s+.*?seconds?)\s*:?\s*$",
    re.IGNORECASE,
)

USER_ATTRS = ("data-testid", "data-test", "data-testid")
USER_VALUES = ("user-message", "user-editor", "user-message-content")
ASSISTANT_VALUES = ("assistant-message-text", "collapsible-text")
#: balise des tours user dans le DOM transcript 2026
USER_CDS = "UserMessage"

#: position absolue (0-based) d'une rangee du transcript virtualise
ROW_INDEX_ATTR_RE = re.compile(r"data-index=['\"](\d+)['\"]")

#: prefixe des jetons de remplacement markdown (post-nettoyage)
_TOKEN = "@@AICV_CLAUDE_%s_%d@@"

#: hotes/chemins des favicons et icones d'interface (jamais du contenu)
_ICON_SRC_RE = re.compile(r"(gstatic\.com|google\.com/s2/favicons|/favicon)", re.IGNORECASE)

#: libelle d'en-tete de bloc de code = langage (sinon UI/fichier)
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9+#.-]+$")


def merge_transcript_rows(fragments: List[str], header_html: str = "") -> str:
    """Fusionne les rangees de transcript accumulees en un HTML parsable.

    Claude virtualise le fil : a chaque position de scroll le DOM ne rend
    qu'une fenetre de `[data-testid='transcript-row']`. Chaque rangee porte
    `data-index`, sa position absolue (0-based) dans le fil. On deduplique par
    index (en gardant le rendu le plus complet) puis on trie par index croissant
    pour restituer la chronologie, independamment du sens du scroll.

    `header_html` (selecteur de modele...) est reinjecte en tete pour conserver
    les metadonnees de page dans le HTML reconstruit.
    """
    best: Dict[int, str] = {}
    for fragment in fragments:
        match = ROW_INDEX_ATTR_RE.search(fragment)
        if not match:
            continue
        index = int(match.group(1))
        previous = best.get(index)
        if previous is None or len(fragment) > len(previous):
            best[index] = fragment
    if not best:
        return ""
    ordered = [best[index] for index in sorted(best)]
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        + (header_html or "")
        + "".join(ordered)
        + "</body></html>"
    )


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
        + [f"div[data-cds='{USER_CDS}']"]
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
    TITLE_SELECTORS = (
        "[data-testid='chat-header-title']",
        "header h1",
        "h1[class*='truncate']",
    )

    #: noeud de contenu privilegie (prose markdown) de chaque role
    ASSISTANT_CONTENT_SELECTORS = (
        ".standard-markdown",
        ".prose",
        "[data-perf-reply-text]",
    )
    USER_CONTENT_SELECTORS = (
        "[data-testid='user-message']",
        ".cds-user-message-body",
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
            content_node = self._content_node(node, role) or node
            content = self._content_text(content_node)
            content = self._drop_thinking_labels(content)
            if role == "user":
                attachments = self._attachments_markdown(turn)
                if attachments:
                    content = f"{content}\n\n{attachments}".strip() if content else attachments
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

    # -- selection du contenu ------------------------------------------------

    @classmethod
    def _content_node(cls, turn: Tag, role: str) -> Optional[Tag]:
        """Noeud porteur du texte utile (prose markdown) pour un tour."""
        selectors = (
            cls.ASSISTANT_CONTENT_SELECTORS
            if role == "assistant"
            else cls.USER_CONTENT_SELECTORS
        )
        for sel in selectors:
            try:
                found = turn.select_one(sel)
            except Exception:
                found = None
            if found is not None:
                return found
        return turn

    # -- rendu markdown ------------------------------------------------------

    @classmethod
    def _content_text(cls, el: Optional[Tag]) -> str:
        """Texte markdown d'un contenu Claude (listes, tableaux, titres...).

        ``BaseParser.text_of`` aplatit les ``<ul>/<ol>``, ``<table>``,
        ``<h1..h6>``, ``<blockquote>``, ``<hr>`` et les mises en forme inline
        (``strong``/``em``/``del``/``code``). On les convertit en markdown sur
        une copie avant l'extraction, puis on reinjecte les remplacements apres
        le nettoyage (qui supprime l'indentation des lignes) afin de conserver
        listes imbriquees et separateurs.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        replacements: Dict[str, str] = {}

        cls._drop_ui_nodes(node)
        cls._preprocess_code_blocks(node)
        cls._normalize_images(node)
        cls._inline_markdown(node)

        # blockquotes : traiter les plus externes (recursion du contenu)
        for index, quote in enumerate(node.find_all("blockquote")):
            if quote.find_parent("blockquote") is not None:
                continue
            inner = cls._content_text(quote)
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in inner.splitlines()
            )
            token = _TOKEN % ("QUOTE", index)
            replacements[token] = quoted
            quote.replace_with(NavigableString(f"\n{token}\n"))

        for index, table in enumerate(node.find_all("table")):
            token = _TOKEN % ("TABLE", index)
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = _TOKEN % ("LIST", index)
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(f"\n{token}\n"))

        for index, heading in enumerate(
            node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ):
            level = int(heading.name[1])
            title = re.sub(r"\s*\n\s*", " ", cls._inline_text(heading)).strip()
            token = _TOKEN % ("HEADING", index)
            replacements[token] = f"{'#' * level} {title}".rstrip()
            heading.replace_with(NavigableString(f"\n{token}\n"))

        for hr in node.find_all("hr"):
            hr.replace_with(NavigableString("\n@@AICV_CLAUDE_HR@@\n"))

        text = super().text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text.replace("@@AICV_CLAUDE_HR@@", "---")

    @classmethod
    def _drop_ui_nodes(cls, node: Tag) -> None:
        """Retire les libelles lecteur d'ecran / elements caches hors contenu."""
        for sel in (
            "h2.sr-only",
            ".sr-only",
            "[class*='screen-reader']",
            "[class*='visually-hidden']",
            "[aria-hidden='true']",
        ):
            for tag in list(node.select(sel)):
                try:
                    tag.decompose()
                except Exception:
                    pass

    @classmethod
    def _preprocess_code_blocks(cls, node: Tag) -> None:
        """Langage des blocs de code : recopie dans ``data-language``.

        Claude affiche le langage dans un en-tete ``div.text-text-500``
        ("python") au-dessus du ``<pre>`` ; ``BaseParser`` ne lit que les classes
        ``language-*`` et ``data-language``. On recopie le libelle (si c'est bien
        un langage) puis on retire l'en-tete pour qu'il ne pollue pas le texte.
        """
        for pre in node.find_all("pre"):
            if pre.find_parent("pre") is not None:
                continue
            wrapper = pre.find_parent(attrs={"aria-label": re.compile(r"^Code\b")})
            if wrapper is None:
                continue
            label_el = wrapper.select_one("div[class*='text-text-500']")
            if label_el is None:
                continue
            label = re.sub(r"\s+", " ", label_el.get_text(" ", strip=True)).strip()
            code = pre.find("code")
            target = code if code is not None else pre
            classes = " ".join(target.get("class") or [])
            if label and "language-" not in classes and _LANGUAGE_RE.match(label):
                target["data-language"] = label.lower()
        for label_el in node.select("div[class*='text-text-500']"):
            label_el.decompose()

    @classmethod
    def _normalize_images(cls, node: Tag) -> None:
        """Images de contenu : URL absolue ; icones d'interface retirees.

        Claude sert les pieces jointes via des URL relatives ``/api/...`` que le
        telechargeur d'images (limite aux URL ``http(s)``) ignorerait : on les
        absolutise sur le domaine claude.ai. Les favicons d'apercu de
        connecteurs (<= 32 px) sont des elements d'interface, pas du contenu.
        """
        for img in node.find_all("img"):
            if (img.get("aria-hidden") or "").lower() == "true":
                img.decompose()
                continue
            src = (img.get("src") or "").strip()
            if src.startswith("/"):
                src = "https://claude.ai" + src
                img["src"] = src
            width = cls._px(img.get("width"))
            height = cls._px(img.get("height"))
            if width and height and width < 64 and height < 64:
                img.decompose()
                continue
            if not (img.get("alt") or "").strip() and _ICON_SRC_RE.search(src):
                img.decompose()

    @staticmethod
    def _px(value: Any) -> int:
        try:
            return int(str(value or "0").replace("px", ""))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _inline_markdown(cls, node: Tag) -> None:
        """Code en ligne, gras, italique et barre -> markdown (hors ``pre``)."""
        for code in node.find_all("code"):
            if code.find_parent("pre") is not None:
                continue
            content = code.get_text().strip()
            if content:
                code.replace_with(NavigableString(f"`{content}`"))
            else:
                code.decompose()

        emphases = node.find_all(["b", "strong", "i", "em", "del", "s", "strike"])
        markers = {
            "b": "**",
            "strong": "**",
            "i": "*",
            "em": "*",
            "del": "~~",
            "s": "~~",
            "strike": "~~",
        }
        # du plus profond au plus externe : preserve l'imbrication
        for el in sorted(emphases, key=lambda t: len(list(t.parents)), reverse=True):
            if el.find_parent("pre") is not None:
                continue
            inner = cls._inline_text(el)
            if inner:
                marker = markers[el.name]
                el.replace_with(NavigableString(f"{marker}{inner}{marker}"))
            else:
                el.decompose()

    @classmethod
    def _inline_text(cls, el: Optional[Tag]) -> str:
        """Texte markdown inline (sans nouvelle structure de blocs)."""
        if el is None:
            return ""
        return cls.text_of(copy.copy(el))

    @classmethod
    def _table_markdown(cls, table: Tag) -> str:
        """Tableau HTML -> lignes markdown ``| ... |`` avec separateur."""
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            rows.append([cls._inline_text(c).strip() for c in cells])
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
    def _render_list(cls, lst: Tag, depth: int = 0) -> List[str]:
        """``<ul>/<ol>`` -> lignes markdown, en gerant les checklists."""
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
            checkbox = li_copy.find("input", attrs={"type": "checkbox"})
            checked = checkbox is not None and checkbox.has_attr("checked")
            if checkbox is not None:
                checkbox.decompose()
            text = re.sub(r"\s*\n\s*", " ", cls._content_text(li_copy)).strip()
            if checkbox is not None:
                marker = "- [x]" if checked else "- [ ]"
            elif ordered:
                marker = f"{start + offset}."
            else:
                marker = "-"
            lines.append(f"{'  ' * depth}{marker} {text}")
            for sub in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(cls._render_list(sub, depth + 1))
        return lines

    # -- pieces jointes ------------------------------------------------------

    @classmethod
    def _attachments_markdown(cls, root: Tag) -> str:
        """Pieces jointes d'un tour user (images et fichiers) en markdown.

        Claude rend chaque piece jointe dans ``[data-cds='MessageAttachments']``
        avant la bulle de texte. Les images sont referencees en markdown (URL
        absolutisee) ; les fichiers non-image par leur nom. Les PDF anciens
        (vignette ``file-thumbnail`` sans ``data-cds``) utilisent l'``alt``.
        """
        entries: List[str] = []
        seen = set()

        def add(entry: str) -> None:
            if entry and entry not in seen:
                seen.add(entry)
                entries.append(entry)

        for container in root.select("[data-cds='MessageAttachments']"):
            for item in container.find_all(recursive=False):
                cds = item.get("data-cds") or ""
                if cds == "MessageAttachmentsImage":
                    name = cls._attachment_name(item)
                    img = item.find("img")
                    src = (img.get("src") or "").strip() if img is not None else ""
                    if src.startswith("/"):
                        src = "https://claude.ai" + src
                    if src.startswith(("http://", "https://")):
                        alt = name or (img.get("alt") or "").strip() or "image"
                        add(f"![{cls._md_escape_label(alt)}]({src})")
                    elif name:
                        add(name)
                elif cds == "MessageAttachmentsFile":
                    name = cls._attachment_name(item)
                    if not name:
                        name = item.get_text(" ", strip=True).splitlines()[0].strip() if item.get_text(strip=True) else ""
                    add(name)
                else:
                    # vignette ancienne (PDF) : pas de data-cds, nom dans l'alt
                    img = item.find("img")
                    name = (img.get("alt") or "").strip() if img is not None else ""
                    if name:
                        add(name)
        return "\n\n".join(entries)

    @classmethod
    def _attachment_name(cls, item: Tag) -> str:
        """Nom de fichier d'une piece jointe (attribut ``title`` ou ``sr-only``)."""
        titled = item.find(attrs={"title": True})
        if titled is not None and (titled.get("title") or "").strip():
            return titled["title"].strip()
        sr = item.select_one(".sr-only")
        if sr is not None:
            return sr.get_text(" ", strip=True)
        return ""

    # -- helpers -----------------------------------------------------------

    @classmethod
    def text_of(cls, el: Optional[Tag]) -> str:
        """Texte propre en preservant le code inline (`` `code` ``).

        `BaseParser.text_of` ne convertit que les blocs ``<pre>`` en fences :
        le code inline rendu par Claude (``<code>total</code>``) perdait ses
        backticks, alors que le schema exige un `texte` markdown complet. On
        transforme ici les ``<code>`` hors ``<pre>`` en code markdown avant le
        nettoyage commun.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        cls._inline_markdown(node)
        return super().text_of(node)

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
        attrs = (
            turn.get("data-testid") or "",
            turn.get("data-test") or "",
            turn.get("data-cds") or "",
        )
        if any(v in attrs for v in USER_VALUES) or USER_CDS in attrs:
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
        # statut de tour 2026 : le label de reflexion y est resume
        for status in turn.select("[data-cds='TurnStatus'], [data-testid='TurnStatus']"):
            if THINKING_LABEL_RE.match(status.get_text(" ", strip=True)):
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
