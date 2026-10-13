"""Classe abstraite BaseParser : HTML -> Conversation du schema standardise.

Les parsers travaillent sur des chaines HTML (issues de `page.content()` ou de
fixtures) avec BeautifulSoup et des selecteurs CSS — les memes selecteurs que
les services passent a Playwright pour les attentes/scrolls.
"""

from __future__ import annotations

import copy
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from ..schema import (
    Conversation,
    ConversationRef,
    Message,
    SchemaError,
    normalize_messages,
)
from ..utils.logging import get_logger, log_fields

log = get_logger("parsers")

ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
SPACE_RE = re.compile(r"[ \t]+")
NEWLINES_RE = re.compile(r"\n{3,}")

# Selecteurs CSS des listes de conversations (surcharge par service)
LOGIN_URL_PARTS = (
    "accounts.google.com",
    "chatgpt.com/auth",
    "claude.ai/__clerc",
    "perplexity.ai/.auth",
    "/sign-in",
    "/login",
    "/oauth",
    "/billing",
)

DROP_TAGS = ("script", "style", "noscript", "iframe", "button", "svg", "textarea")

#: caracteres de controle de direction (RTL/LTR) injectes par certaines UIs
DIRECTION_MARKS_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def clean_ui_title(title: Optional[str]) -> Optional[str]:
    """Titre utilisable : sans marques de direction, nbsp, ni libelle generique."""
    if not title:
        return None
    title = DIRECTION_MARKS_RE.sub("", title).replace("\u00a0", " ").strip()
    title = re.sub(r"\s+", " ", title)
    if not title or title.lower() in ("google gemini", "gemini", "perplexity", "chatgpt"):
        return None
    return title
ACTION_SELECTORS = (
    "button",
    "[role='button']",
    ".action-bar",
    "message-actions",
    "[data-testid$='-menu']",
    "[aria-label*='Copy' i]",
    "[aria-label*='Regenerate' i]",
    "[aria-label*='like' i]",
    # UI invisible hors hover (timestamps de bulle, labels decoratifs)
    "span[class*='opacity-0']",
    "[aria-hidden='true']",
    # libelles lecteur d'ecran (ex: Gemini "Vous avez dit ...")
    ".cdk-visually-hidden",
    "[class*='screen-reader']",
    # marqueurs de citation inline (sources extraites a part -> metadata)
    "span.citation",
    ".citation-nbsp",
)


class ParseError(Exception):
    """HTML de conversation non reconnaissable (DOM change, login wall...)."""


class BaseParser(ABC):
    """Interface commune des parsers DOM par plateforme."""

    service_name: str = ""
    #: selecteurs CSS des liens de conversation dans la sidebar
    link_selectors: tuple = ()
    #: regex pour extraire l'id depuis l'href des liens
    conversation_id_pattern: re.Pattern = re.compile(r"/[a-z-]+/([A-Za-z0-9_-]{6,})")
    #: selecteurs d'un message (utilises par le service pour attendre le rendu)
    message_selectors: tuple = ()

    # -- API publique ---------------------------------------------------------

    @abstractmethod
    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        """HTML d'une page de conversation -> Conversation validee."""

    def parse_links(self, html: str, base_url: str) -> List[ConversationRef]:
        """Extrait les references de conversations d'une sidebar."""
        soup = self.make_soup(html)
        refs: List[ConversationRef] = []
        seen: set = set()
        for sel in self.link_selectors:
            for a in soup.select(sel):
                href = a.get("href") or a.get("data-href") or ""
                match = self.conversation_id_pattern.search(href)
                if not match:
                    continue
                conv_id = match.group(1)
                if conv_id in seen:
                    continue
                seen.add(conv_id)
                title = self._link_title(a)
                section = self._link_section(a)
                refs.append(
                    ConversationRef(
                        service=self.service_name,
                        id=conv_id,
                        url=urljoin(base_url, href),
                        title=title,
                        section=section,
                    )
                )
        return refs

    # -- aides BeautifulSoup -----------------------------------------------------

    @staticmethod
    def make_soup(html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "html.parser")

    @classmethod
    def select_first(cls, root: Tag, selectors: List[str] | tuple) -> Optional[Tag]:
        for sel in selectors:
            try:
                found = root.select_one(sel)
            except Exception:
                found = None
            if found is not None:
                return found
        return None

    @classmethod
    def select_all_any(cls, root: Tag, selectors: List[str] | tuple) -> List[Tag]:
        """Tous les elements du premier selecteur qui renvoie un resultat."""
        for sel in selectors:
            try:
                found = root.select(sel)
            except Exception:
                found = []
            if found:
                return found
        return []

    @classmethod
    def text_of(cls, el: Optional[Tag]) -> str:
        """Texte propre : blocs `pre` en fenced code, br -> newline, sans UI.

        bs4: `copy.copy(tag)` est une copie profonde -> l'original reste intact
        (on decompose des noeuds pendant la lecture de la structure).
        """
        if el is None:
            return ""
        node = copy.copy(el)
        cls._clean_tree(node)
        return cls._normalize_text(node.get_text())

    @classmethod
    def _clean_tree(cls, node: Tag) -> None:
        """Convertit l'arbre en texte markdown (in place), sans balises d'UI."""
        cls._latex_nodes_to_text(node)
        cls._button_images_to_markdown(node)
        cls._citations_to_markdown(node)

        for tag in node.find_all(DROP_TAGS):
            tag.decompose()
        for sel in ACTION_SELECTORS:
            for tag in node.select(sel):
                tag.decompose()

        cls._pre_to_fence(node)
        cls._images_to_markdown(node)
        cls._links_to_markdown(node)

        for br in node.find_all("br"):
            br.replace_with(NavigableString("\n"))
        for block in node.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
            block.insert_before(NavigableString("\n"))
            block.insert_after(NavigableString("\n"))

    @classmethod
    def _latex_nodes_to_text(cls, node: Tag) -> None:
        """LaTeX : conserver la source TeX avant toute suppression (KaTeX rendu,
        Gemini data-math, ChatGPT data-math-source)."""
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
                NavigableString(f"\n\\[{tex}\\]\n" if display else f" \\({tex}\\) ")
            )

    @classmethod
    def _button_images_to_markdown(cls, node: Tag) -> None:
        """Images de contenu piegees dans un bouton d'apercu : les remonter en
        markdown avant que le bouton ne soit supprime (DROP_TAGS)."""
        for button in node.find_all("button"):
            content_images = [
                img for img in button.find_all("img")
                if (img.get("src") or "").startswith(("http://", "https://"))
                and (img.get("aria-hidden") or "").lower() != "true"
                and cls._img_is_content(img)
            ]
            if content_images:
                markdown = "\n".join(
                    f"![{(img.get('alt') or 'image').strip() or 'image'}]({img.get('src').strip()})"
                    for img in content_images
                )
                button.replace_with(NavigableString(f"\n{markdown}\n"))

    @classmethod
    def _citations_to_markdown(cls, node: Tag) -> None:
        """Citations inline (Perplexity) : conserver le lien avant suppression."""
        for citation in node.select("span.citation"):
            url = citation.get("data-pplx-citation-url") or ""
            label = citation.get_text(" ", strip=True)
            if url and label:
                citation.replace_with(NavigableString(f" [{label}]({url}) "))

    @classmethod
    def _pre_to_fence(cls, node: Tag) -> None:
        """Blocs ``<pre>`` -> fences markdown avec la langue detectee."""
        for pre in node.find_all("pre"):
            # Mistral : le bloc est encapsule avec un en-tete (langue) a retirer
            root = pre.find_parent(
                class_=lambda value: bool(value) and "markdown-fenced-code-root" in value
            ) or pre
            code = pre.find("code")
            lang = ""
            for element in (code, pre):
                if element is None:
                    continue
                for cls_name in element.get("class") or []:
                    if cls_name.startswith("language-"):
                        lang = cls_name.split("-", 1)[1]
                        break
                if not lang:
                    lang = element.get("data-language") or element.get("data-lang") or ""
                if lang:
                    break
            if not lang and root is not pre:
                label = root.select_one("span[class*='text-subtle'], span.font-medium")
                if label is not None:
                    lang = label.get_text(" ", strip=True).splitlines()[0].strip()
            code_text = (code or pre).get_text().rstrip()
            root.replace_with(NavigableString(f"\n```{lang}\n{code_text}\n```\n"))

    @classmethod
    def _images_to_markdown(cls, node: Tag) -> None:
        """Images distantes -> markdown (blob:/data: inutiles en export, ignores)."""
        for img in node.find_all("img"):
            src = img.get("src") or ""
            if src.startswith(("http://", "https://")):
                alt = (img.get("alt") or "image").strip() or "image"
                img.replace_with(NavigableString(f"![{alt}]({src})"))
            else:
                img.decompose()

    @classmethod
    def _links_to_markdown(cls, node: Tag) -> None:
        """Liens -> markdown ``[texte](url)`` ; ancres/relatifs = texte seul."""
        for a in node.find_all("a"):
            href = (a.get("href") or "").strip()
            text = a.get_text(" ", strip=True)
            if href.startswith(("http://", "https://", "mailto:")):
                if text.startswith("![") and text.endswith(")"):
                    label = text  # lien autour d'une image : markdown deja pret
                else:
                    label = cls._md_escape_label(text) or href
                a.replace_with(
                    NavigableString(f"[{label}]({cls._md_escape_href(href)})")
                )
            elif text:
                a.replace_with(NavigableString(text))
            else:
                a.unwrap()

    @staticmethod
    def _md_escape_label(label: str) -> str:
        """Libelle de lien markdown : crochets echappes, pas de retour ligne."""
        return label.replace("\n", " ").replace("[", "\\[").replace("]", "\\]")

    @staticmethod
    def _md_escape_href(href: str) -> str:
        """URL de lien markdown : parentheses encodees (sinon lien casse)."""
        return href.replace("(", "%28").replace(")", "%29")

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.replace("\u00a0", " ").replace("\u202f", " ")
        text = ZERO_WIDTH_RE.sub("", text)
        lines = []
        in_fence = False
        for line in text.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                lines.append(line.rstrip())
            elif in_fence:
                # preserver l'indentation du code
                lines.append(line.rstrip())
            else:
                lines.append(SPACE_RE.sub(" ", line).strip())
        text = "\n".join(lines)
        text = NEWLINES_RE.sub("\n\n", text)
        return text.strip()

    # -- rendu markdown commun (contenus, tableaux, listes, inline) ----------

    #: chatgpt/perplexity rognent l'espace final des lignes (item vide -> « - »)
    _rstrip_list_lines: bool = False

    #: prefixe des jetons internes de remplacement (surcharge par parseur)
    _content_token_prefix: str = "AICV"

    @classmethod
    def _list_marker(cls, li: Tag, li_copy: Tag, *, ordered: bool, number: int) -> str:
        """Marqueur markdown d'un item (``-``, ``1.``, ``- [x]``).

        ``li`` est l'item d'origine (recherche des cases a cocher) ; ``li_copy``
        est la copie de travail, listes imbriquees retirees : les surcharges
        peuvent y supprimer les cases a cocher avant l'extraction du texte.
        """
        return f"{number}." if ordered else "-"

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
            li_copy = copy.copy(li)
            for sub in li_copy.find_all(["ul", "ol"]):
                sub.decompose()
            marker = cls._list_marker(
                li, li_copy, ordered=ordered, number=start + offset
            )
            text = re.sub(r"\s*\n\s*", " ", cls._content_text(li_copy)).strip()
            line = f"{'  ' * depth}{marker} {text}"
            lines.append(line.rstrip() if cls._rstrip_list_lines else line)
            for sub in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(cls._render_list(sub, depth + 1))
        return lines

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
    def _inline_text(cls, el: Optional[Tag]) -> str:
        """Texte markdown inline (sans structure de blocs)."""
        if el is None:
            return ""
        return cls.text_of(copy.copy(el))

    @classmethod
    def _inline_code_text(cls, code: Tag) -> str:
        """Texte d'un code en ligne (surcharge : Claude garde les retours ligne)."""
        return code.get_text(" ", strip=True)

    @classmethod
    def _inline_markdown(cls, node: Tag) -> None:
        """Code en ligne, gras, italique et barre -> markdown (hors ``pre``)."""
        for code in node.find_all("code"):
            if code.find_parent("pre") is not None:
                continue
            content = cls._inline_code_text(code)
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
    def _content_token(cls, kind: str, index: Optional[int] = None) -> str:
        """Jeton interne remplace apres le nettoyage (jamais visible en sortie)."""
        suffix = "" if index is None else f"_{index}"
        return f"@@{cls._content_token_prefix}_{kind}{suffix}@@"

    @classmethod
    def _content_preprocess(cls, node: Tag) -> None:
        """Transformations du contenu avant les blocs markdown (surcharge)."""

    @classmethod
    def _extra_content_blocks(cls, node: Tag, replacements: Dict[str, str]) -> None:
        """Blocs specifiques inseres entre blockquotes et tableaux (surcharge)."""

    @classmethod
    def _heading_markdown(cls, heading: Tag) -> str:
        """Titre HTML -> ligne markdown ``# ...`` (texte inline)."""
        level = int(heading.name[1])
        title = re.sub(r"\s*\n\s*", " ", cls._inline_text(heading)).strip()
        return f"{'#' * level} {title}".rstrip()

    @classmethod
    def _content_text(cls, el: Optional[Tag]) -> str:
        """Texte markdown d'un contenu (blockquotes, tableaux, listes, titres, hr).

        ``text_of`` aplatit la structure de blocs ; on la convertit en markdown
        sur une copie, puis on reinjecte les remplacements apres le nettoyage
        (qui supprime l'indentation des lignes) afin de conserver listes
        imbriquees et separateurs. Les parseurs fournissent leurs pre-traitements
        et formats via ``_content_preprocess`` / ``_extra_content_blocks`` /
        ``_heading_markdown`` / ``_content_token_prefix``.
        """
        if el is None:
            return ""
        node = copy.copy(el)
        replacements: Dict[str, str] = {}
        cls._content_preprocess(node)

        # blockquotes : traiter les plus externes (recursion du contenu)
        for index, quote in enumerate(node.find_all("blockquote")):
            if quote.find_parent("blockquote") is not None:
                continue
            inner = cls._content_text(quote)
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in inner.splitlines()
            )
            token = cls._content_token("QUOTE", index)
            replacements[token] = quoted
            quote.replace_with(NavigableString(f"\n{token}\n"))

        cls._extra_content_blocks(node, replacements)

        for index, table in enumerate(node.find_all("table")):
            token = cls._content_token("TABLE", index)
            replacements[token] = cls._table_markdown(table)
            table.replace_with(NavigableString(f"\n{token}\n"))

        for index, lst in enumerate(node.find_all(["ul", "ol"])):
            if lst.find_parent(["ul", "ol"]) is not None:
                continue  # traitee avec sa liste parente
            token = cls._content_token("LIST", index)
            replacements[token] = "\n".join(cls._render_list(lst))
            lst.replace_with(NavigableString(f"\n{token}\n"))

        for index, heading in enumerate(
            node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ):
            token = cls._content_token("HEADING", index)
            replacements[token] = cls._heading_markdown(heading)
            heading.replace_with(NavigableString(f"\n{token}\n"))

        for hr in node.find_all("hr"):
            hr.replace_with(NavigableString(f"\n{cls._content_token('HR')}\n"))

        text = cls.text_of(node)
        for token, markdown in replacements.items():
            text = text.replace(token, markdown)
        return text.replace(cls._content_token("HR"), "---")

    @staticmethod
    def extract_title(soup: BeautifulSoup, *selectors: str) -> Optional[str]:
        for sel in selectors:
            try:
                el = soup.select_one(sel)
            except Exception:
                el = None
            if el is not None and el.get_text(strip=True):
                return el.get_text(strip=True)
        og = soup.find("meta", property="og:title") or soup.find(
            "meta", attrs={"name": "title"}
        )
        if og and og.get("content"):
            return str(og["content"]).strip()
        if soup.title and soup.title.get_text(strip=True):
            return soup.title.get_text(strip=True)
        return None

    @classmethod
    def _link_title(cls, a: Tag) -> Optional[str]:
        for attr in ("title", "aria-label"):
            value = a.get(attr)
            if value and str(value).strip():
                return str(value).strip()
        text = a.get_text(" ", strip=True)
        return text or None

    @classmethod
    def _link_section(cls, a: Tag) -> Optional[str]:
        """Date-groupe le plus proche au-dessus du lien (Today/Yesterday...)."""
        current = a
        for _ in range(4):
            parent = current.parent
            if parent is None:
                break
            heading = parent.find_previous(["h2", "h3", "h4", "summary"])
            if heading is not None:
                label = heading.get_text(" ", strip=True)
                if label and len(label) < 40:
                    return label
            current = parent
        return None

    def log_parse(self, conv: Conversation, **fields: Any) -> None:
        log_fields(
            log,
            20,
            f"parsed {self.service_name} conversation",
            extra={
                "conversation_id": conv.conversation_id,
                "messages": len(conv.messages),
                **fields,
            },
        )

    @staticmethod
    def check(conv: Conversation) -> Conversation:
        """Normalise (alternance des roles) puis valide la conversation."""
        conv.messages = normalize_messages(conv.messages)
        try:
            conv.derive_timestamps()
            conv.validate()
        except SchemaError as exc:
            raise ParseError(str(exc)) from exc
        return conv

    @staticmethod
    def _img_is_content(img: Tag) -> bool:
        """Image de contenu (pas une icone) : dimension >= 64px si connue."""
        for attr in ("width", "height"):
            try:
                value = int(str(img.get(attr) or "0").replace("px", ""))
            except ValueError:
                value = 0
            if value >= 64:
                return True
        # dimensions inconnues : on suppose une image de contenu
        return not (img.get("width") or img.get("height"))

    @staticmethod
    def attachments_markdown(el: Tag, url_filter=None) -> str:
        """Images distantes d'un tour en markdown (y compris dans un bouton).

        Les visuels de contenu (pieces jointes, images generees) sont souvent
        places dans un `<button>` (apercu) qui serait sinon supprime.
        """
        found: List[str] = []
        seen = set()
        for img in el.find_all("img"):
            if (img.get("aria-hidden") or "").lower() == "true":
                continue
            if img.find_parent(attrs={"aria-hidden": "true"}) is not None:
                continue
            src = (img.get("src") or "").strip()
            if not src.startswith(("http://", "https://")):
                continue
            if url_filter and not url_filter(src):
                continue
            alt = (img.get("alt") or "image").strip() or "image"
            markdown = f"![{alt}]({src})"
            if markdown not in seen:
                seen.add(markdown)
                found.append(markdown)
        return "\n\n".join(found)

    @staticmethod
    def majority(values: List[Any]) -> Optional[Any]:
        """Valeur la plus frequente (ignoree si vide). None si aucune."""
        present = [value for value in values if value]
        return max(set(present), key=present.count) if present else None

    @staticmethod
    def msg(
        role: str,
        content: str,
        timestamp: Any = None,
        metadata: Optional[Dict] = None,
        *,
        message_id: str = "",
        model: Optional[str] = None,
    ) -> Message:
        """Construit un message standardise (texte + code_blocks derives)."""
        return Message(
            role=role,
            texte=content,
            timestamp=timestamp,
            metadata=metadata or {},
            message_id=message_id,
            model=model,
        )
