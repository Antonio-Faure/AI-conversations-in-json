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


def merge_thread_messages(items: List[Dict[str, Any]]) -> str:
    """Reconstruit le HTML d'un fil Perplexity a partir des tours accumules.

    Le fil est virtualise : le navigateur ne monte qu'une fenetre de messages.
    Le service remonte le conteneur scrollable en memorisant chaque message
    (cle = role + debut de contenu), en gardant le HTML le plus long et la
    derniere position verticale connue. On trie ici par position absolue pour
    retrouver l'ordre chronologique, puis on re-emballe chaque message dans un
    conteneur dedie (le parser retrouve ainsi les sources au bon endroit).

    ``items`` : liste de dicts ``{key, role, html, pos}``.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for item in items or []:
        key = item.get("key")
        html = item.get("html")
        if not key or not html:
            continue
        rows[key] = item
    if not rows:
        return ""
    ordered = sorted(
        rows.values(),
        key=lambda it: (it.get("pos") or 0, it.get("key") or ""),
    )
    parts = ['<html><body><div class="aicv-perplexity-thread">']
    for item in ordered:
        parts.append(
            '<div class="aicv-ppl-msg" data-role="%s">%s</div>'
            % (item.get("role") or "message", item["html"])
        )
    parts.append("</div></body></html>")
    return "\n".join(parts)


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
        Les pieces jointes (apercu image dans un bouton, nom de fichier) sont
        conservees.

        Certains descendants de la piece jointe portent aussi `opacity-0`
        (vignette d'apercu) : on ne decompose pas les `img` pour que le
        markdown de l'image soit produit par `text_of`.
        """
        work = copy.copy(node)
        for toolbar in work.select("[class*='opacity-0']"):
            if toolbar.name == "img":
                continue
            toolbar.decompose()
        for button in work.select("[role='button'][title]"):
            url = (button.get("title") or "").strip()
            if url.startswith(("http://", "https://")):
                button.replace_with(NavigableString(url))
        cls._preserve_file_attachments(work)
        return cls.text_of(work)

    @classmethod
    def _assistant_text(cls, node: Tag) -> str:
        """Corps de la reponse, sans l'en-tete de workflow (« Recherche terminee »).

        Le tour `final-text` encapsule un en-tete d'etape, le corps rendu
        (`[data-renderer='lm']`) et un footer d'actions. On ne garde que le(s)
        corps ; listes, tableaux, titres et separateurs sont rendus en markdown.
        """
        bodies = node.select("[data-renderer='lm']")
        if bodies:
            parts: List[str] = []
            for body in bodies:
                text = cls._content_text(body)
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
        return cls._content_text(work)

    # -- rendu markdown (listes, tableaux, titres, code) -----------------------

    @classmethod
    def _content_text(cls, el: Optional[Tag]) -> str:
        """Texte markdown d'un contenu Perplexity.

        ``BaseParser.text_of`` aplatit les ``<ul>/<ol>``, ``<table>`` et
        ``<h1..h6>``. On les convertit en markdown sur une copie avant
        l'extraction, puis on reinjecte les remplacements apres nettoyage
        (qui supprime l'indentation des lignes) afin de conserver listes
        imbriquees et separateurs.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        replacements: Dict[str, str] = {}
        cls._code_languages(node)

        # blockquotes : traiter les plus externes (recursion du contenu)
        for index, quote in enumerate(node.find_all("blockquote")):
            if quote.find_parent("blockquote") is not None:
                continue
            inner = cls._content_text(quote)
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in inner.splitlines()
            )
            token = f"@@AICV_PPL_QUOTE_{index}@@"
            replacements[token] = quoted
            quote.replace_with(NavigableString(f"\n{token}\n"))

        for index, table in enumerate(node.find_all("table")):
            token = f"@@AICV_PPL_TABLE_{index}@@"
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = f"@@AICV_PPL_LIST_{index}@@"
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(f"\n{token}\n"))

        for index, heading in enumerate(
            node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ):
            level = int(heading.name[1])
            title = re.sub(r"\s*\n\s*", " ", cls._inline_text(heading)).strip()
            token = f"@@AICV_PPL_HEADING_{index}@@"
            replacements[token] = f"{'#' * level} {title}".strip()
            heading.replace_with(NavigableString(f"\n{token}\n"))

        for hr in node.find_all("hr"):
            hr.replace_with(NavigableString("\n@@AICV_PPL_HR@@\n"))

        text = cls.text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text.replace("@@AICV_PPL_HR@@", "---")

    @staticmethod
    def _code_languages(node: Tag) -> None:
        """Injecte la langue des blocs de code (``figcaption span``) dans le `pre`."""
        for pre in node.find_all("pre"):
            if pre.get("data-language"):
                continue
            cap = pre.find("figcaption")
            label = cap.find("span") if cap is not None else None
            if label is None and cap is not None:
                label = cap
            if label is None:
                continue
            lang = label.get_text(" ", strip=True).split()
            if lang:
                pre["data-language"] = lang[0]

    @classmethod
    def _inline_text(cls, el: Optional[Tag]) -> str:
        """Texte markdown inline (sans structure de blocs)."""
        if el is None:
            return ""
        return cls.text_of(copy.copy(el))

    @classmethod
    def _table_markdown(cls, table: Tag) -> str:
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
    def _render_list(cls, lst: Tag, depth: int = 0) -> List[str]:
        """Liste HTML -> lignes markdown (puces, numeros, cases a cocher)."""
        ordered = lst.name == "ol"
        try:
            start = int(lst.get("start") or 1)
        except (TypeError, ValueError):
            start = 1
        lines: List[str] = []
        for offset, li in enumerate(lst.find_all("li", recursive=False)):
            checkbox = li.select_one("[data-pplx-task-checkbox]")
            if checkbox is not None:
                button = checkbox.find("button")
                checked = str(
                    button.get("aria-checked") if button is not None else ""
                ).lower() == "true"
                marker = f"- [{'x' if checked else ' '}]"
            else:
                marker = f"{start + offset}." if ordered else "-"
            li_copy = copy.copy(li)
            for sub in li_copy.find_all(["ul", "ol"]):
                sub.decompose()
            text = re.sub(r"\s*\n\s*", " ", cls._content_text(li_copy)).strip()
            lines.append(f"{'  ' * depth}{marker} {text}".rstrip())
            for sub in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(cls._render_list(sub, depth + 1))
        return lines

    #: noms de fichiers joints rendus en `<button>` (sans apercu image)
    _FILE_LABEL_RE = re.compile(
        r"^[\w .()'\-]+\.(txt|md|csv|json|pdf|png|jpe?g|gif|webp|svg|"
        r"mp3|wav|m4a|mp4|mov|webm|docx?|xlsx?|pptx?|zip|rtf|odt)$",
        re.IGNORECASE,
    )

    @classmethod
    def _preserve_file_attachments(cls, node: Tag) -> None:
        """Remet le nom des pieces jointes non-image (bouton sans apercu)."""
        for button in node.find_all("button"):
            if button.find("img") is not None:
                continue  # apercu image : laisse a `text_of`
            label = button.get_text(" ", strip=True)
            if label and cls._FILE_LABEL_RE.match(label):
                button.replace_with(NavigableString(f"\n\n{label}\n\n"))

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
