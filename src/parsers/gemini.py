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

#: libelles de l'en-tete d'un bloc de code qui ne sont pas un langage
#: (Gemini affiche "Extrait de code", "Resultat du code"...)
_CODE_UI_LABELS = frozenset(
    {
        "",
        "code",
        "extrait de code",
        "code snippet",
        "resultat",
        "resultat du code",
        "résultat",
        "résultat du code",
        "sortie",
        "output",
    }
)
#: un langage tient en un seul jeton (Python, C++, HTML, YAML...)
_LANGUAGE_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+#.\-]*$")

#: conteneur d'un tour complet (requete + reponse) dans le DOM Gemini
TURN_SELECTOR = ".conversation-container"
#: selecteur de la reponse textuelle d'un tour
TURN_ANSWER_SELECTOR = (
    "model-response message-content .model-response-text, "
    "model-response message-content, message-content"
)


#: noeuds d'accessibilite qui dupliquent la requete dans un tour virtualise
_UI_TEXT_SELECTORS = (
    ".cdk-visually-hidden",
    "[class*='screen-reader']",
    "[aria-hidden='true']",
)


def _turn_user_key(turn_soup: BeautifulSoup) -> str:
    """Clef d'identite d'un tour : requete utilisateur normalisee (+ images).

    L'id du conteneur change d'un rendu virtualise a l'autre ; on s'appuie
    donc sur le contenu de la requete pour reconnaitre un meme tour. On retire
    au prealable les libelles lecteur d'ecran (``cdk-visually-hidden``) qui
    dupliquent le texte et feraient diverger la clef d'un exemplaire a l'autre.
    """
    user = turn_soup.select_one("user-query")
    if user is None:
        return ""
    user = copy.copy(user)
    for sel in _UI_TEXT_SELECTORS:
        for tag in user.select(sel):
            tag.decompose()
    text = re.sub(r"\s+", " ", user.get_text(" ", strip=True)).strip().lower()
    images = " ".join(
        (img.get("src") or img.get("alt") or "") for img in user.find_all("img")
    )
    return f"{text}|{images}"


def _turn_has_answer(turn_soup: BeautifulSoup) -> bool:
    answer = turn_soup.select_one(TURN_ANSWER_SELECTOR)
    return bool(answer is not None and answer.get_text(strip=True))


def merge_turn_fragments(
    fragments: List[str], order: Optional[List[str]] = None
) -> str:
    """Fusionne des tours autonomes en un HTML unique, sans doublon.

    Gemini virtualise le fil : en remontant, le DOM rend parfois plusieurs
    exemplaires d'un meme tour (fenetres re-rendues, variantes de reponse). On
    garde, pour chaque requete utilisateur, l'occurrence la plus complete
    (reponse non vide).

    ``order`` (optionnel) est la liste des tours du DOM final dans l'ordre
    logique du fil ; elle sert de reference d'ordre, les tours absents etant
    ajoutes a la suite dans leur ordre de decouverte. Sans elle, on retombe sur
    l'ordre de decouverte de ``fragments`` (comportement historique).

    Retourne un document HTML minimal parsable par :meth:`GeminiParser.parse`.
    """
    parsed: List[tuple] = []
    for fragment in list(order or []) + list(fragments):
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

    # Ordre logique : d'abord les tours du DOM final (`order`), puis ceux
    # seulement accumules (`fragments`) dans leur ordre de decouverte.
    ordered_keys: List[str] = []
    placed: set = set()
    for fragment in order or []:
        soup = BeautifulSoup(fragment, "html.parser")
        if soup.select_one("user-query") is None:
            continue
        key = _turn_user_key(soup)
        if key in best and key not in placed:
            ordered_keys.append(key)
            placed.add(key)
    for key, entry in sorted(best.items(), key=lambda item: item[1][1]):
        if key not in placed:
            ordered_keys.append(key)
            placed.add(key)

    selected = [best[key][2] for key in ordered_keys]
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

        # Chaque `.conversation-container` = un tour (requete + reponse). On
        # apparie a l'interieur du conteneur plutot que globalement : un tour
        # dont la reponse est vide (image pure, etat de rendu) reste ainsi
        # rattache a sa requete et ne colle pas deux requetes consecutives.
        containers = soup.select(".conversation-container")
        pairs: List[tuple] = []
        allow_empty_assistant = bool(containers)
        if containers:
            for container in containers:
                user = container.select_one("user-query")
                assistant = container.select_one("model-response")
                if user is None and assistant is None:
                    continue
                if user is not None:
                    pairs.append((user, "user"))
                if assistant is not None:
                    pairs.append((assistant, "assistant"))
                elif user is not None:
                    # requete sans noeud de reponse monte : on garde un tour
                    # vide pour ne pas fusionner avec la requete suivante
                    pairs.append((None, "assistant"))
        else:
            user_nodes = self.select_all_any(soup, self.USER_SELECTORS[:6])
            assistant_nodes = self.select_all_any(soup, self.ASSISTANT_SELECTORS[:4])
            flat = [(n, "user") for n in user_nodes] + [
                (n, "assistant") for n in assistant_nodes
            ]
            positions = {}
            for el in soup.descendants:
                positions.setdefault(id(el), len(positions))
            flat.sort(key=lambda p: positions.get(id(p[0]), 1 << 30))
            pairs = list(flat)

        messages: List[Any] = []
        model: Optional[str] = extra.get("model")
        for node, role in pairs:
            if node is None:
                messages.append(self.msg("assistant", "", None, {"tokens": None}))
                continue
            if role == "user":
                content_el = self.select_first(node, self.CONTENT_OF_USER)
            else:
                content_el = self.select_first(node, self.CONTENT_OF_ASSISTANT)
            content = self._content_text(content_el if content_el is not None else node)
            if role == "user":
                attach_root = node.find_parent("user-query") or node
                images = self.attachments_markdown(
                    attach_root, url_filter=self._is_content_image
                )
                files = self._file_attachments_markdown(attach_root)
                attachments = "\n\n".join(part for part in (images, files) if part)
                if attachments and attachments not in content:
                    content = f"{content}\n\n{attachments}".strip() if content else attachments
            if not content:
                # reponse vide d'un tour : on conserve la place pour ne pas
                # fusionner deux requetes (seulement si elle suit une requete)
                keep_empty = (
                    allow_empty_assistant
                    and role == "assistant"
                    and messages
                    and messages[-1].role == "user"
                )
                if not keep_empty:
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
        """Texte markdown d'un contenu Gemini (listes, tableaux, titres...).

        ``BaseParser.text_of`` aplatit les ``<ul>/<ol>``, ``<table>``,
        ``<h1..h6>``, ``<blockquote>``, ``<hr>``, le gras/italique, le code en
        ligne et la LaTeX ``data-math``. On normalise tout cela en markdown sur
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
        cls._latex_markdown(node)
        cls._inline_markdown(node)
        cls._generated_files(node)

        # blockquotes : traiter les plus externes (recursion du contenu)
        for index, quote in enumerate(node.find_all("blockquote")):
            if quote.find_parent("blockquote") is not None:
                continue
            inner = cls._content_text(quote)
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in inner.splitlines()
            )
            token = f"@@AICV_GEMINI_QUOTE_{index}@@"
            replacements[token] = quoted
            quote.replace_with(NavigableString(f"\n{token}\n"))

        for index, table in enumerate(node.find_all("table")):
            token = f"@@AICV_GEMINI_TABLE_{index}@@"
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = f"@@AICV_GEMINI_LIST_{index}@@"
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(f"\n{token}\n"))

        for index, heading in enumerate(
            node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ):
            level = int(heading.name[1])
            title = re.sub(r"\s*\n\s*", " ", cls._inline_text(heading)).strip()
            token = f"@@AICV_GEMINI_HEADING_{index}@@"
            replacements[token] = f"{'#' * level} {title}".rstrip()
            heading.replace_with(NavigableString(f"\n{token}\n"))

        for hr in node.find_all("hr"):
            hr.replace_with(NavigableString("\n@@AICV_GEMINI_HR@@\n"))

        text = cls.text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text.replace("@@AICV_GEMINI_HR@@", "---")

    # -- rendu inline (avant `BaseParser.text_of`) -----------------------------

    @classmethod
    def _drop_ui_nodes(cls, node) -> None:
        """Retire les libelles lecteur d'ecran / elements caches hors contenu."""
        for sel in (
            ".cdk-visually-hidden",
            "[class*='screen-reader']",
            "[aria-hidden='true']",
        ):
            for tag in list(node.select(sel)):
                try:
                    tag.decompose()
                except Exception:
                    pass

    @classmethod
    def _preprocess_code_blocks(cls, node) -> None:
        """Injecte le langage des ``<code-block>`` Gemini dans le ``<pre>``.

        Gemini enveloppe le code dans ``<code-block>`` avec un en-tete
        ``.code-block-decoration``; ``BaseParser.text_of`` ne connait que les
        classes ``language-*``. On recopie le libelle (s'il s'agit bien d'un
        langage) dans ``data-language`` puis on retire les en-tetes pour qu'ils
        ne polluent pas le texte.
        """
        for pre in node.find_all("pre"):
            if pre.find_parent("pre") is not None:
                continue
            code = pre.find("code")
            classes = list(code.get("class") or []) if code is not None else []
            is_output = "code-result-container" in classes or (
                code is not None
                and code.get("data-test-id") == "code-output-stdout-stderr"
            )
            if not is_output:
                deco = pre.find_previous(class_="code-block-decoration")
                block = pre.find_parent("code-block")
                if deco is not None and (
                    block is None or deco.find_parent("code-block") is block
                ):
                    lang = cls._normalize_language(deco.get_text(" ", strip=True))
                    if lang:
                        pre["data-language"] = lang
        for deco in node.select(".code-block-decoration"):
            deco.decompose()
        for divider in node.find_all("mat-divider"):
            divider.decompose()

    @staticmethod
    def _normalize_language(label: str) -> str:
        """Libelle d'en-tete -> identifiant de langage (ou vide si UI)."""
        text = re.sub(r"\s+", " ", (label or "")).strip()
        if text.lower() in _CODE_UI_LABELS or re.search(r"\s", text):
            return ""
        if not _LANGUAGE_TOKEN_RE.match(text):
            return ""
        return text.lower()

    @classmethod
    def _latex_markdown(cls, node) -> None:
        """``data-math`` Gemini -> ``$...$`` / ``$$...$$``.

        ``BaseParser._clean_tree`` transforme sinon ces noeuds en ``\\(...\\)``
        / ``\\[...\\]`` ; on impose le markdown ``$`` attendu par le schema.
        """
        for el in node.select("[data-math], [data-math-source]"):
            tex = (el.get("data-math") or el.get("data-math-source") or "").strip()
            if not tex:
                continue
            classes = " ".join(el.get("class") or [])
            display = (
                el.name == "div"
                or "math-block" in classes
                or el.get("data-math-display") == "block"
            )
            el.replace_with(
                NavigableString(f"\n$${tex}$$\n" if display else f" ${tex}$ ")
            )

    @classmethod
    def _inline_markdown(cls, node) -> None:
        """Code en ligne, gras, italique et barre -> markdown (hors ``pre``)."""
        for code in node.find_all("code"):
            if code.find_parent("pre") is not None:
                continue
            text = code.get_text(" ", strip=True)
            if text:
                code.replace_with(NavigableString(f"`{text}`"))
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
    def _generated_files(cls, node) -> None:
        """Puce de fichier genere -> nom du fichier, sans icone ni bouton."""
        for generated in node.find_all("generated-file"):
            name_el = generated.select_one(".file-name-lr")
            name = ""
            if name_el is not None:
                name = (name_el.get("title") or name_el.get_text(" ", strip=True)).strip()
            if not name:
                name = generated.get_text(" ", strip=True)
            generated.replace_with(NavigableString(f"\n\n{name}\n\n" if name else "\n"))
        # icones de type de fichier (ex: /32/type/text/csv) : hors contenu
        for img in node.find_all("img"):
            src = img.get("src") or ""
            if "drive-thirdparty.googleusercontent.com" in src:
                img.decompose()

    @classmethod
    def _inline_text(cls, el) -> str:
        """Texte markdown inline (sans nouvelle structure de blocs)."""
        if el is None:
            return ""
        return cls.text_of(copy.copy(el))

    @classmethod
    def _table_markdown(cls, table) -> str:
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
            text = re.sub(r"\s*\n\s*", " ", cls._content_text(li_copy)).strip()
            marker = f"{start + offset}." if ordered else "-"
            lines.append(f"{'  ' * depth}{marker} {text}")
            for sub in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(cls._render_list(sub, depth + 1))
        return lines

    @classmethod
    def _file_attachments_markdown(cls, root) -> str:
        """Noms des pieces jointes non-image (pastille ``uploaded-file``)."""
        names: List[str] = []
        for div in root.select("[data-test-id='uploaded-file']"):
            button = div.find("button") or div
            name = (button.get("aria-label") or "").strip()
            if not name:
                filename = div.select_one(".filename-label")
                extension = div.select_one(".extension-label")
                stem = filename.get_text(" ", strip=True) if filename else ""
                ext = extension.get_text(" ", strip=True).lower() if extension else ""
                name = f"{stem}.{ext}" if stem and ext else stem
            if name and name not in names:
                names.append(name)
        return "\n\n".join(names)

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
