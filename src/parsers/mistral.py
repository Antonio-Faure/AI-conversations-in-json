"""Parser DOM Mistral (chat.mistral.ai).

Structure observee (2026) :
  - tour de message : <div data-message-author-role="user|assistant"
                           data-message-id="..." data-message-version="0">
  - user      : <div class="select-text"><span class="whitespace-pre-wrap">...</span></div>
  - assistant : parties <div data-message-part-type="answer"> (a garder) et
                "reasoning" (reflexion interne, exclue) ; le texte est dans
                .markdown-container-style
  - pieces jointes : vignettes/images dans la grille du tour ; les noms/types/
                URL complets sont aussi presents dans le payload RSC Next.js
                (`self.__next_f.push`) sous forme de `"files"` par message.
  - titre : <title> de la page (ex: "Estimation tokens ...")
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import NavigableString

from ..schema import Conversation
from .base import BaseParser, ParseError

#{ deux modes : /chat (chatbot) et /work (agentique). Les projets
# (/chat/projects/<id>) ne sont pas des conversations et sont exclus par le
# motif d'id (uuid juste apres /chat/ ou /work/).
ID_RE = re.compile(r"/(?:chat|work)/([0-9a-fA-F-]{8,})")

#: origine de l'app (les images generees sont referencees en chemin relatif
#: `/cdn-cgi/image/...` : il faut les absolutiser avant telechargement)
BASE_URL = "https://chat.mistral.ai"


def merge_message_fragments(
    rows: Dict[str, str],
    edges: List[Any],
    seq: List[str],
) -> str:
    """Fusionne les tours accumules pendant la remontee d'un fil Mistral.

    Mistral peut virtualiser un long fil : a chaque position de scroll le DOM
    ne monte qu'une fenetre de tours (`div[data-message-author-role]`). Chaque
    tour est deduplique par `data-message-id` (on garde le rendu le plus
    complet). Faute d'indice absolu, l'ordre logique est reconstitue a partir
    des aretes ``tour -> tour suivant`` observees dans le DOM ; les tours non
    relies sont ajoutes dans l'ordre de decouverte (``seq``).

    Retourne un document HTML minimal parsable par :meth:`MistralParser.parse`.
    """
    if not rows:
        return ""
    succ: Dict[str, List[str]] = {}
    pred: Dict[str, set] = {}
    for edge in edges or []:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            continue
        left, right = str(edge[0]), str(edge[1])
        if left not in rows or right not in rows or left == right:
            continue
        succ.setdefault(left, [])
        if right not in succ[left]:
            succ[left].append(right)
        pred.setdefault(right, set()).add(left)

    # chaine lineaire : on part des tours sans predecesseur (tete du fil)
    order: List[str] = []
    seen: set = set()
    starts = [key for key in seq if key in rows and not pred.get(key)]
    starts += [key for key in seq if key in rows and key not in starts]
    for start in starts:
        node: Optional[str] = start
        while node is not None and node not in seen:
            seen.add(node)
            order.append(node)
            following = [n for n in succ.get(node, []) if n not in seen]
            node = following[0] if following else None
    order += [key for key in seq if key in rows and key not in seen]

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        + "".join(rows[key] for key in order)
        + "</body></html>"
    )


# Payload RSC Next.js : chaque fragment est une chaine JSON (`self.__next_f.push`).
_RSC_PUSH_RE = re.compile(r"self\.__next_f\.push\(\[1,(\"(?:[^\"\\]|\\.)*\")\]\)")
# Debut d'un objet message dans le payload (cle `role` en premier).
_MSG_START_RE = re.compile(r'\{\s*"role"\s*:\s*"(?:user|assistant)"')


class MistralParser(BaseParser):
    service_name = "mistral"

    link_selectors = (
        "a[href^='/chat/']",
        "a[href^='/work/']",
        "a[href*='/chat/']",
        "a[href*='/work/']",
    )
    conversation_id_pattern = ID_RE

    message_selectors = ("div[data-message-author-role]",)

    TITLE_SELECTORS = (
        "header h1",
        "[data-testid='chat-title']",
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
        attachments = self._attachments_index(html)

        messages: List[Any] = []
        for container in soup.select("[data-message-author-role]"):
            role = (container.get("data-message-author-role") or "").lower()
            if role not in ("user", "assistant", "system", "tool"):
                role = "assistant"
            message_id = str(container.get("data-message-id") or "")
            text = self._text_of_turn(container, role, attachments.get(message_id))
            if not text:
                continue
            messages.append(
                self.msg(
                    role,
                    text,
                    None,
                    {"tokens": None} if role == "assistant" else {},
                    message_id=message_id,
                )
            )

        if not messages:
            raise ParseError("mistral: aucun message extrait — session ou DOM modifie")

        title = self.extract_title(soup, *self.TITLE_SELECTORS)
        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Mistral conversation",
            messages=messages,
        )
        self.log_parse(conv, title=title)
        return self.check(conv)

    # -- pieces jointes --------------------------------------------------------

    @classmethod
    def _attachments_index(cls, html: str) -> Dict[str, List[Dict[str, Any]]]:
        """Index `message_id -> files` depuis le payload RSC de la page.

        Le DOM n'expose le nom que des pastilles de fichiers et l'URL que des
        images ; le payload contient nom + type + URL signee de chaque piece
        jointe. En cas d'absence (HTML simplifie, format change), on retombe sur
        le DOM via :meth:`_attachments_markdown`.
        """
        if "__next_f" not in html:
            return {}
        parts: List[str] = []
        for match in _RSC_PUSH_RE.finditer(html):
            try:
                parts.append(json.loads(match.group(1)))
            except ValueError:
                continue
        payload = "".join(parts)
        if not payload:
            return {}
        index: Dict[str, List[Dict[str, Any]]] = {}
        for match in _MSG_START_RE.finditer(payload):
            raw = cls._json_object_at(payload, match.start())
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            message_id = data.get("id")
            files = data.get("files")
            if not message_id or not isinstance(files, list):
                continue
            clean = [f for f in files if isinstance(f, dict)]
            if clean:
                index.setdefault(str(message_id), clean)
        return index

    @staticmethod
    def _json_object_at(text: str, start: int) -> Optional[str]:
        """Sous-chaine de l'objet JSON equilibre commencant a `start`."""
        depth = 0
        in_string = False
        escaped = False
        index = start
        while index < len(text):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
            index += 1
        return None

    @classmethod
    def _attachments_markdown(cls, container, files: Optional[List[Dict[str, Any]]]) -> str:
        """Markdown des pieces jointes d'un tour (payload prioritaire, repli DOM)."""
        entries: List[str] = []
        if files:
            for file in files:
                name = str(file.get("name") or "").strip()
                url = str(file.get("url") or "").strip()
                if file.get("type") == "image" and url:
                    entries.append(f"![{cls._md_escape_label(name) or 'image'}]({url})")
                elif url:
                    entries.append(f"[{cls._md_escape_label(name) or url}]({url})")
                elif name:
                    entries.append(name)
        else:
            for img in container.find_all("img"):
                src = (img.get("src") or "").strip()
                if not src.startswith(("http://", "https://")):
                    continue
                if (img.get("aria-hidden") or "").lower() == "true":
                    continue
                if not cls._img_is_content(img):
                    continue
                entries.append(f"![image]({src})")
            for span in container.select("p[class*='text-truncator'] span"):
                name = span.get_text(" ", strip=True)
                if name:
                    entries.append(name)
        seen = set()
        unique = [e for e in entries if not (e in seen or seen.add(e))]
        return "\n\n".join(unique)

    # -- texte markdown --------------------------------------------------------

    @classmethod
    def _text_of_turn(
        cls,
        container,
        role: str,
        files: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if role == "assistant":
            parts = container.select("[data-message-part-type='answer']")
            texts = [cls._content_text(part) for part in parts]
            text = "\n\n".join(t for t in texts if t).strip()
            if not text:
                # reponses rendues en "canvas"/document (sans partie answer)
                canvas = container.select_one("div[class*='pt-3'][class*='pb-4']")
                text = cls._content_text(canvas).strip() if canvas is not None else ""
            images = cls._generated_images_markdown(container, text)
            if images:
                return f"{text}\n\n{images}".strip() if text else images
            return text
        # user : le corps du message (hors boutons/actions) + pieces jointes
        body = container.select_one(".select-text") or container
        text = cls._content_text(body)
        attachments = cls._attachments_markdown(container, files)
        if attachments:
            return f"{text}\n\n{attachments}".strip() if text else attachments
        return text

    @classmethod
    def _generated_images_markdown(cls, container, existing: str = "") -> str:
        """Images de contenu d'un tour assistant (ex: images generees).

        Le DOM les reference en chemin relatif (`/cdn-cgi/image/...`) ; on les
        absolutise pour permettre leur telechargement par le pipeline. Le tour
        « image seule » doit rester non vide, sinon deux messages `user`
        consecutifs seraient fusionnes par `normalize_messages` et le tour
        serait perdu.
        """
        entries: List[str] = []
        seen: set = set()
        for img in container.find_all("img"):
            if (img.get("aria-hidden") or "").lower() == "true":
                continue
            if img.find_parent(attrs={"data-slot": "avatar"}) is not None:
                continue
            src = (img.get("src") or "").strip()
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(BASE_URL, src)
            if not src.startswith(("http://", "https://")):
                continue
            if src in existing:
                continue
            alt = (img.get("alt") or "image").strip() or "image"
            markdown = f"![{cls._md_escape_label(alt)}]({src})"
            if markdown not in seen:
                seen.add(markdown)
                entries.append(markdown)
        return "\n\n".join(entries)

    @classmethod
    def _content_text(cls, el) -> str:
        """Texte markdown d'un contenu (listes, tableaux, titres, `---`).

        ``BaseParser.text_of`` aplatit les ``<ul>/<ol>``, ``<table>``,
        ``<h1..h6>``, ``<blockquote>`` et ``<hr>``. On les convertit en markdown
        sur une copie avant l'extraction, puis on reinjecte les remplacements
        apres nettoyage (qui supprime l'indentation des lignes) afin de
        conserver listes imbriquees et separateurs.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        replacements: Dict[str, str] = {}
        cls._latex_to_markdown(node)

        # blockquotes : les traiter en premier (recursion pour leur contenu) ;
        # ne garder que les plus externes.
        for index, quote in enumerate(node.find_all("blockquote")):
            if quote.find_parent("blockquote") is not None:
                continue
            inner = cls._content_text(quote)
            quoted = "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
            token = f"@@AICV_MISTRAL_QUOTE_{index}@@"
            replacements[token] = quoted
            quote.replace_with(NavigableString(f"\n{token}\n"))

        # tableaux rich-ui : la source HTML d'origine est stockee dans un attribut
        for index, wrapper in enumerate(node.select("[data-rich-table-inner-html]")):
            token = f"@@AICV_MISTRAL_RICH_TABLE_ATTR_{index}@@"
            replacements[token] = cls._rich_table_from_attr(wrapper)
            wrapper.replace_with(NavigableString(f"\n{token}\n"))

        for index, table in enumerate(node.select("[role='table']")):
            token = f"@@AICV_MISTRAL_RICH_TABLE_{index}@@"
            replacements[token] = cls._rich_table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, table in enumerate(node.find_all("table")):
            token = f"@@AICV_MISTRAL_TABLE_{index}@@"
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = f"@@AICV_MISTRAL_LIST_{index}@@"
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(f"\n{token}\n"))

        for index, heading in enumerate(node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])):
            level = int(heading.name[1])
            title = re.sub(r"\s*\n\s*", " ", cls._content_text(heading)).strip()
            token = f"@@AICV_MISTRAL_HEADING_{index}@@"
            replacements[token] = f"{'#' * level} {title}"
            heading.replace_with(NavigableString(f"\n{token}\n"))

        for hr in node.find_all("hr"):
            hr.replace_with(NavigableString("\n@@AICV_MISTRAL_HR@@\n"))

        text = cls.text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text.replace("@@AICV_MISTRAL_HR@@", "---")

    @classmethod
    def _latex_to_markdown(cls, node) -> None:
        """KaTeX -> ``$...$`` / ``$$...$$`` (corrige le ``$`` ouvrant seul de base).

        ``BaseParser._clean_tree`` remplace le KaTeX inline par `` $tex `` sans
        ``$`` fermant ; on le fait ici pour preserver un markdown conforme.
        """
        for katex in node.select("span.katex"):
            annotation = katex.find("annotation", attrs={"encoding": "application/x-tex"})
            if annotation is None:
                continue
            tex = annotation.get_text().strip()
            if not tex:
                continue
            display = katex.find_parent(class_="katex-display") is not None
            katex.replace_with(
                NavigableString(f"\n$${tex}$$\n" if display else f" ${tex}$ ")
            )

    @classmethod
    def _inline_text(cls, el) -> str:
        """Texte markdown inline (LaTeX corrige) sans structure de blocs."""
        if el is None:
            return ""
        node = copy.copy(el)
        cls._latex_to_markdown(node)
        return cls.text_of(node)

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

    @staticmethod
    def _is_action_cell(cell) -> bool:
        """Cellule de la colonne d'actions collante (copie) a exclure."""
        classes = " ".join(cell.get("class") or [])
        return "sticky" in classes and "end-0" in classes

    @classmethod
    def _rich_table_from_attr(cls, wrapper) -> str:
        """Tableau rich-ui via l'attribut ``data-rich-table-inner-html``.

        Mistral conserve dans cet attribut le HTML d'origine du tableau
        markdown (``<thead>/<tbody>``) : c'est la source la plus fidele.
        """
        inner = wrapper.get("data-rich-table-inner-html") or ""
        table = cls.make_soup(inner).find("table") if inner else None
        markdown = cls._table_markdown(table) if table is not None else ""
        title = (wrapper.get("data-rich-table-title") or "").strip()
        if title and markdown:
            return f"{title}\n\n{markdown}"
        return markdown or title

    @classmethod
    def _rich_table_markdown(cls, table) -> str:
        """Tableau ``rich-ui`` Mistral (grille de ``role=cell``) -> markdown.

        Mistral ne rend pas les tableaux markdown en ``<table>`` mais en grille
        ``div[role=table]`` dont les cellules sont des enfants directs
        (``columnheader``/``cell``) suivies d'une colonne d'actions collante.
        """
        cells = [
            cell
            for cell in table.find_all(recursive=False)
            if cell.get("role") in ("columnheader", "cell")
        ]
        if not cells:
            return ""
        columns = len(cells)
        for index, cell in enumerate(cells):
            if cls._is_action_cell(cell):
                columns = index + 1
                break
        if columns <= 0:
            return ""
        lines: List[str] = []
        for row_index, start in enumerate(range(0, len(cells), columns)):
            row = cells[start : start + columns]
            values = []
            for cell in row:
                if cls._is_action_cell(cell):
                    continue
                values.append(re.sub(r"\s*\n\s*", " ", cls._inline_text(cell)).strip())
            if not values:
                continue
            lines.append("| " + " | ".join(values) + " |")
            if row_index == 0:
                lines.append("| " + " | ".join(["---"] * len(values)) + " |")
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
