"""Parser DOM ChatGPT (chatgpt.com).

Deux generations de DOM sont gerees :

- **UI historique** : <div data-message-id="<uuid>" data-message-author-role="user|assistant"
  data-message-model-slug="gpt-4o"> <div class="markdown ...">...</div>, tours
  `[data-testid^='conversation-turn']`. Timestamps injectes par le service via
  les donnees internes React, passees en `extra`.

- **UI « DIL » (2026)** : plus de `data-message-author-role`/`data-message-id`.
  Un tour `[data-turn-key]` contient des unites de message
  `[data-chatgpt-search-unit-key="<turn>:<n>:user|assistant"]` ; le texte
  assistant est dans `[data-markdown-text-style="assistant-message"]`, le texte
  utilisateur dans `div.text-size-chat.whitespace-pre-wrap`. Le raisonnement et
  les etapes d'outil vivent dans des en-tetes `[class*='activity-header']`
  (« Worked for Xs ») rattaches au tour. Les fichiers joints sont des
  `[class*='resource-card']` et certains blocs de code n'ont pas de `<pre>`
  (`CodeSurface` > `code.whitespace-pre!`). Les images markdown distantes ne
  sont plus rendues en `<img>` (bouton placeholder sans URL) : elles sont perdues.
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

#: libelles du panneau de raisonnement/COT (ChatGPT localise le titre)
REASONING_LABEL_RE = re.compile(
    r"^(thought for|worked for|reasoned for|thinking|"
    r"réflexion|raisonnement|a réfléchi|a travaillé|"
    r"travail pendant|réfléchi pendant)[\s:]",
    re.IGNORECASE,
)

#: cartes canvas/artefacts : conteneur texte editable titre par ChatGPT
CANVAS_ID_PREFIX = "textdoc-message-"

#: UI « DIL » (2026) : unites de message sans data-message-author-role
NEW_UNIT_SELECTORS = (
    "[data-chatgpt-search-unit-key]",
    "[data-content-search-unit-key]",
)
#: role porte par l'attribut de l'en-tete assistant (nouvelle UI)
NEW_ROLE_ATTR = "data-conversation-role"
NEW_ASSISTANT_TEXT = "[data-markdown-text-style='assistant-message']"
NEW_USER_BUBBLE = "[data-user-message-bubble]"
NEW_USER_TEXT = "div.text-size-chat.whitespace-pre-wrap"
#: conteneur de tour de la nouvelle UI
NEW_TURN_ATTR = "data-turn-key"
#: en-tetes d'activite (raisonnement « Worked for Xs » + etapes d'outil)
NEW_ACTIVITY_HEADER_SELECTOR = "[class*='activity-header']"

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

    message_selectors = NEW_UNIT_SELECTORS + (
        "div[data-message-author-role='assistant']",
        "div[data-message-id]",
        "[data-testid^='conversation-turn']",
    )

    TURN_SELECTORS = (
        "[data-turn-key]",
        "[data-chatgpt-search-unit-key]",
        "[data-message-author-role][data-message-id]",
        "[data-message-author-role]",
        "div[data-message-id]",
        "[data-testid^='conversation-turn']",
        "main article[role='article']",
    )
    CONTENT_SELECTORS = (
        NEW_ASSISTANT_TEXT,
        NEW_USER_TEXT,
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
            # le raisonnement/COT, le canvas et les sources vivent dans le tour
            # complet ; les pieces jointes et l'image du message restent dans
            # l'unite de message (ne pas melanger les deux messages du tour).
            scope = self._turn_container(turn)
            content_el = self.select_first(turn, self.CONTENT_SELECTORS)
            content = self._content_text(content_el)
            # pieces jointes / images generees (souvent hors du markdown)
            attachments = self.attachments_markdown(
                turn, url_filter=self._is_content_image
            )
            extra_blocks = [attachments, self._file_tiles(turn)]
            if role == "assistant":
                # le canvas est un frere du message dans le tour : ne l'injecter
                # que dans la reponse assistant (pas dans le message utilisateur)
                extra_blocks.append(self._canvas_content(scope))
            for extra_block in extra_blocks:
                if extra_block and extra_block not in content:
                    content = f"{content}\n\n{extra_block}".strip() if content else extra_block
            if not content:
                # nouvelle UI : un tour image externe (markdown `![]()` non
                # resolu) est rendu en bouton placeholder sans URL ; on garde
                # le message vide plutot que de fusionner les roles voisins.
                is_new_unit = (
                    turn.get("data-chatgpt-search-unit-key") is not None
                    or turn.get("data-content-search-unit-key") is not None
                )
                if not is_new_unit:
                    continue
            # raisonnement/COT, artefacts/canvas, sources et outils (hors .markdown)
            reasoning = self._reasoning_text(scope) if role == "assistant" else ""
            artifacts = self._canvas_artifacts(scope) if role == "assistant" else []
            sources = self._sources(scope) if role == "assistant" else []
            tools = self._tools(scope) if role == "assistant" else []
            mid = self._message_id_of(turn)
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
            message = self.msg(
                role,
                content,
                timestamp,
                meta,
                message_id=str(mid or ""),
                model=slug,
            )
            if reasoning:
                message.reasoning = reasoning
            if artifacts:
                message.artifacts = artifacts
            if sources:
                message.sources = sources
            if tools:
                message.tools = tools
            messages.append(message)

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
            last_message_at=extra.get("last_message_at"),
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
        """Tours dans l'ordre du document (nouvelles et anciennes UI).

        Nouvelle UI « DIL » : chaque message est une unite
        `[data-chatgpt-search-unit-key="<tour>:<n>:user|assistant"]` (le role
        est le dernier segment). Ancienne UI : `[data-message-author-role]` et
        les tours image `[data-testid^='conversation-turn'][data-turn]`.
        """
        positions: Dict[Any, int] = {}
        for element in soup.descendants:
            positions.setdefault(id(element), len(positions))

        turns = []
        seen: set = set()
        for el in soup.select(", ".join(NEW_UNIT_SELECTORS)):
            if id(el) in seen or el.get("style") == "display: none;":
                continue
            # une unite peut etre imbriquee dans une autre (l'unite utilisateur
            # porte `data-chatgpt-search-unit-key`, le bloc interne seulement
            # `data-content-search-unit-key`) : garder la plus externe.
            if el.find_parent(
                attrs={"data-chatgpt-search-unit-key": True}
            ) is not None or el.find_parent(
                attrs={"data-content-search-unit-key": True}
            ) is not None:
                continue
            role = self._new_role_of(el)
            if role is None:
                continue
            seen.add(id(el))
            turns.append((positions.get(id(el), 0), el, role))
        if turns:
            turns.sort(key=lambda item: item[0])
            return [(turn, role) for _, turn, role in turns]

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

    @classmethod
    def _new_role_of(cls, el) -> Optional[str]:
        """Role d'une unite de message de la nouvelle UI, sinon None."""
        for attr in ("data-chatgpt-search-unit-key", "data-content-search-unit-key"):
            key = el.get(attr)
            if not key:
                continue
            role = str(key).rsplit(":", 1)[-1].lower()
            if role in ("user", "assistant", "system", "tool"):
                return role
        heading = el.select_one(f"[{NEW_ROLE_ATTR}]")
        if heading is not None:
            role = (heading.get(NEW_ROLE_ATTR) or "").lower()
            if role in ("user", "assistant", "system", "tool"):
                return role
        if el.select_one(NEW_ASSISTANT_TEXT) is not None:
            return "assistant"
        if el.select_one(NEW_USER_BUBBLE) is not None:
            return "user"
        return None

    @staticmethod
    def _message_id_of(el) -> Optional[str]:
        """Identifiant de message (nouvelle UI puis ancienne)."""
        ids = el.get("data-chatgpt-search-message-ids") or ""
        if ids:
            return ids.split()[0]
        if el.get("data-dil-message-id"):
            return el.get("data-dil-message-id")
        holder = el.select_one("[data-chatgpt-selection-message-id]")
        if holder is not None:
            return holder.get("data-chatgpt-selection-message-id")
        if el.get("data-message-id"):
            return el.get("data-message-id")
        return None

    # -- raisonnement, artefacts et pieces jointes ----------------------------

    @staticmethod
    def _turn_container(el):
        """Remonte au tour complet d'un element message.

        Nouvelle UI : ``[data-turn-key]``. Ancienne UI :
        ``[data-testid^='conversation-turn']``. Raisonnement, canvas et sources
        sont des freres du message dans ce conteneur.
        """
        node = el
        while node is not None:
            if node.get(NEW_TURN_ATTR):
                return node
            testid = node.get("data-testid") or ""
            if testid.startswith("conversation-turn"):
                return node
            node = node.parent
        return el

    @classmethod
    def _reasoning_text(cls, turn) -> str:
        """Raisonnement/COT d'un tour assistant ('Thought for Xs').

        Nouvelle UI : les en-tetes d'activite ``[class*='activity-header']``
        portent le libelle « Worked for Xs » (deploye ou replie). Ancienne UI :
        un ``<button>`` dont le libelle commence par ``Thought for``/``Worked
        for`` suivi du contenu deploye ; a defaut (panneau replie), le libelle.
        """
        parts: List[str] = []
        for header in turn.select(NEW_ACTIVITY_HEADER_SELECTOR):
            label = cls._activity_label(header)
            if label and REASONING_LABEL_RE.match(label) and label not in parts:
                parts.append(label)
        if parts:
            return "\n\n".join(parts)
        for button in turn.find_all("button"):
            label = re.sub(r"\s+", " ", button.get_text(" ", strip=True)).strip()
            if not label or not REASONING_LABEL_RE.match(label):
                continue
            panel = button.find_next_sibling()
            content = ""
            if panel is not None and panel.name == "div":
                content = cls._normalize_reasoning(cls.text_of(panel))
            text = content or label
            if text and text not in parts:
                parts.append(text)
        return "\n\n".join(parts)

    @classmethod
    def _activity_label(cls, header) -> str:
        """Libelle d'un en-tete d'activite (cible de ``aria-labelledby``)."""
        label_el = None
        button = header.find("button")
        if button is not None and button.get("aria-labelledby"):
            label_el = header.find(id=button.get("aria-labelledby"))
        if label_el is None:
            label_el = header.select_one("span.truncate") or header.select_one("span")
        if label_el is None:
            return ""
        return re.sub(r"\s+", " ", label_el.get_text(" ", strip=True)).strip()

    @staticmethod
    def _normalize_reasoning(text: str) -> str:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in (text or "").splitlines()]
        return "\n".join(line for line in lines if line).strip()

    @classmethod
    def _canvas_artifacts(cls, turn) -> List[str]:
        """Titres des cartes canvas/artefacts d'un tour."""
        titles: List[str] = []
        for canvas in turn.select(f"[id^='{CANVAS_ID_PREFIX}']"):
            title_el = canvas.select_one(".font-semibold")
            title = (
                re.sub(r"\s+", " ", title_el.get_text(" ", strip=True)).strip()
                if title_el is not None
                else ""
            )
            if title and title not in titles:
                titles.append(title)
        return titles

    @classmethod
    def _canvas_content(cls, turn) -> str:
        """Contenu markdown d'une carte canvas (sinon perdu hors ``.markdown``)."""
        blocks: List[str] = []
        for canvas in turn.select(f"[id^='{CANVAS_ID_PREFIX}']"):
            editor = canvas.select_one("[contenteditable='true']")
            if editor is None:
                editor = canvas.select_one(".markdown") or canvas
            text = cls._content_text(editor).strip()
            if text and text not in blocks:
                blocks.append(text)
        return "\n\n".join(blocks)

    @classmethod
    def _file_tiles(cls, turn) -> str:
        """Pieces jointes (PDF/TXT/CSV/JSON...) -> markdown detectable.

        Ancienne UI : ``button.group/file-tile`` (nom dans ``aria-label`` /
        ``.font-semibold``). Nouvelle UI : ``span[class*='resource-card']`` avec
        un ``button[aria-label="<nom>"][aria-busy]`` et un ``span[title]``.
        Ces noms sont supprimes par ``text_of`` : on les reinjecte comme liens
        (ou texte) pour qu'ils restent dans ``texte``.
        """
        parts: List[str] = []
        seen: set = set()
        tiles = turn.select("[class*='group/file-tile']") + turn.select(
            "[class*='resource-card']"
        )
        for tile in tiles:
            label = (tile.get("aria-label") or "").strip()
            button = tile.find("button", attrs={"aria-label": True})
            if button is not None and not label:
                label = (button.get("aria-label") or "").strip()
            name_el = (
                tile.select_one("span[title]")
                or tile.select_one(".truncate.font-semibold")
                or tile.select_one(".font-semibold")
            )
            name = ""
            if name_el is not None:
                name = (name_el.get("title") or name_el.get_text(" ", strip=True) or "").strip()
            name = re.sub(r"\s+", " ", label or name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            link = tile.select_one("a[href]")
            href = (link.get("href") or "").strip() if link is not None else ""
            markdown = f"[{name}]({href})" if href and href.startswith(("http", "/")) else name
            if markdown not in parts:
                parts.append(markdown)
        return "\n\n".join(parts)

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
        # nouvelle UI : le code inline est un `<span>` (pas un `<code>`)
        for span in node.select("[data-markdown-copy='inline-code']"):
            if span.find_parent("pre") is not None:
                continue
            code = span.get_text()
            if code:
                span.replace_with(NavigableString(f"`{code}`"))

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
        display_attr = (element.get("data-math-display") or "").lower()
        if display_attr in ("true", "block"):
            return True
        if display_attr == "false":
            return False
        if "display:block" in style or "mathBlock" in classes:
            return True
        if "Display-" in classes:  # nouvelle UI : wrapper KaTeX en bloc
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
    def _sources(cls, turn) -> List[str]:
        """Citations web (pastilles) -> ``Message.sources`` (« titre — url »).

        ChatGPT rend chaque source comme une pastille ``webpage-citation-pill``
        dont le lien porte l'URL (``href``/``alt``) et le libelle du site
        (``span.truncate``). On garde une entree par pastille, dedupliquee.
        """
        sources: List[str] = []
        for pill in turn.select("[data-testid='webpage-citation-pill']"):
            link = pill.find("a")
            if link is None:
                continue
            href = (link.get("href") or link.get("alt") or "").strip()
            if not href:
                continue
            label_el = link.select_one("span.truncate")
            label = (
                label_el.get_text(" ", strip=True)
                if label_el is not None
                else link.get_text(" ", strip=True)
            )
            label = re.sub(r"\s+", " ", label).strip()
            # retire le compteur de citations groupees (« +1 »)
            label = re.sub(r"\s*\+\d+$", "", label).strip()
            entry = f"{label} — {href}" if label and label != href else href
            if entry not in sources:
                sources.append(entry)
        return sources

    @classmethod
    def _tools(cls, turn) -> List[str]:
        """Etapes d'appel d'outil -> ``Message.tools``.

        Nouvelle UI : les etapes sont des en-tetes d'activite imbriques
        ``[class*='activity-header']`` dont le resume est rendu en markdown.
        Ancienne UI : cartes ``cot-v5-tool-icon-pile`` (libelle dans le
        ``<button aria-label>`` du groupe, sinon son texte visible).
        """
        tools: List[str] = []
        for header in turn.select(NEW_ACTIVITY_HEADER_SELECTOR):
            label = cls._activity_label(header)
            if label and REASONING_LABEL_RE.match(label):
                continue  # en-tete racine = raisonnement, pas une etape
            md = header.select_one(NEW_ASSISTANT_TEXT) or header.select_one(
                "[class*='MarkdownRoot']"
            )
            text = cls.text_of(md) if md is not None else ""
            if not text:
                text = label
            text = re.sub(r"\s+", " ", text).strip()
            if text and text not in tools:
                tools.append(text)
        if tools:
            return tools
        for pile in turn.select("[data-testid='cot-v5-tool-icon-pile']"):
            group = pile.find_parent("div", class_="group")
            label = ""
            if group is not None:
                button = group.find("button")
                if button is not None:
                    label = (button.get("aria-label") or "").strip()
                if not label:
                    label = group.get_text(" ", strip=True)
            label = re.sub(r"\s+", " ", label).strip()
            if label and label not in tools:
                tools.append(label)
        return tools

    @classmethod
    def _code_languages(cls, node) -> None:
        """Injecte la langue des blocs de code ChatGPT (en-tete de la carte).

        Nouvelle UI : l'en-tete ``div.font-sans`` (libelle dans ``div.truncate``)
        est un frere du ``<pre>`` a l'interieur de ``[class*='CodeBlock']`` ;
        on retire l'en-tete du texte pour ne pas polluer le markdown.
        Ancienne UI : l'en-tete est un descendant du ``<pre>``.
        """
        for pre in node.find_all("pre"):
            if pre.get("data-language"):
                continue
            header = pre.select_one("div.font-sans")
            if header is None:
                block = pre.find_parent(attrs={"data-markdown-copy": "code-block"})
                if block is not None:
                    header = block.select_one("div.font-sans")
            if header is None:
                continue
            label_el = header.find("div", recursive=False) or header.select_one(
                "div.truncate, div.min-w-0"
            )
            if label_el is None:
                continue
            label = re.sub(r"\s+", " ", label_el.get_text(" ", strip=True)).strip()
            if not label or len(label) > 30:
                continue
            pre["data-language"] = cls._normalize_language(label)
            # l'en-tete (langue + boutons de copie) ne doit pas entrer dans le texte
            if header.find_parent("pre") is None:
                header.decompose()
        # nouvelle UI : certains blocs n'ont pas de <pre> (CodeSurface >
        # `code.whitespace-pre!`) : on les rend en fence markdown pour ne pas
        # les perdre (sinon « Plain text » + code inline en backticks).
        for surface in node.select("[data-markdown-copy='code-block']"):
            if surface.find("pre") is not None:
                continue  # deja traite via <pre>
            code = surface.find("code")
            if code is None:
                continue
            header = surface.select_one("div.font-sans")
            label = ""
            if header is not None:
                label_el = header.find("div", recursive=False) or header.select_one(
                    "div.truncate, div.min-w-0"
                )
                if label_el is not None:
                    label = re.sub(
                        r"\s+", " ", label_el.get_text(" ", strip=True)
                    ).strip()
            if not label or label.lower() == "plain text":
                language = ""
            else:
                language = cls._normalize_language(label)
            code_text = code.get_text()
            surface.replace_with(
                NavigableString(f"\n```{language}\n{code_text}\n```\n")
            )

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
