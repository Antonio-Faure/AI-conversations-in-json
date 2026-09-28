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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bs4 import NavigableString, Tag

from ..schema import Conversation, parse_iso, to_iso_z
from .base import BaseParser, ParseError, clean_ui_title

CONV_ID_RE = re.compile(r"/search/([A-Za-z0-9_-]{10,})")
MODEL_RE = re.compile(
    r"(sonar[\w.-]*|Perplexity[\w .-]*(?:Pro|Deep Research|Search)?[\w.-]*)",
    re.IGNORECASE,
)

#: mois francais abreges -> numero (libelles « 25 sept., 19:01 »)
_FRENCH_MONTHS = {
    "janv": 1, "jan": 1, "févr": 2, "fevr": 2, "fév": 2, "fev": 2,
    "mars": 3, "avr": 4, "avril": 4, "mai": 5, "juin": 6,
    "juil": 7, "juillet": 7, "août": 8, "aout": 8, "sept": 9,
    "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}

#: libelle d'horodatage visible d'un message (« 25 sept., 19:01 », « 01:27 »)
_TS_LABEL_RE = re.compile(
    r"^(?:(\d{1,2})\s+([A-Za-zéûôîàèç]+)\.?,?\s+)?(\d{1,2}):(\d{2})$",
    re.IGNORECASE,
)
#: libelle relatif (« Aujourd'hui 11:05 », « Hier 0:34 »)
_TS_RELATIVE_RE = re.compile(
    r"^(aujourd'?hui|hier)\s*,?\s+(\d{1,2}):(\d{2})$", re.IGNORECASE
)


def merge_thread_messages(items: List[Dict[str, Any]]) -> str:
    """Reconstruit le HTML d'un fil Perplexity a partir des tours accumules.

    Le fil est virtualise : le navigateur ne monte qu'une fenetre de messages.
    Le service descend jusqu'au bas puis remonte le conteneur scrollable en
    memorisant chaque message (identite stable par noeud DOM, cf.
    ``_COLLECT_THREAD_JS``), en gardant le HTML le plus long et une position
    absolue decroissante quand on remonte vers les plus anciens. On trie ici par
    position pour retrouver l'ordre chronologique, puis on re-emballe chaque
    message dans un conteneur dedie (le parser retrouve ainsi les sources au bon
    endroit).

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
        # NB : pas de `h1` nu — il attraperait un titre rendu dans le corps
        # d'une reponse (« # Titre principal ») au lieu du nom du fil. Le
        # service recopie le `<title>` de la page dans le HTML accumule.
        "[data-testid='thread-title']",
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

        #: tours de progression seuls (Pro Search) : conteneur `data-workflow-items`
        #: sans corps `lm` ni bulle user, avec un en-tete d'etape. Ils precedent la
        #: reponse et ne sont pas dans le corps `lm`.
        step_positions = sorted(
            (
                positions.get(id(el), 1 << 30),
                el,
            )
            for el in soup.select("div[data-workflow-items]")
            if el.find(attrs={"data-renderer": "lm"}) is None
            and el.select_one("div[class~='group/user-bubble']") is None
            and el.select("[class*='step-header']")
        )

        page_timestamp = self._page_timestamp(soup)
        reference = self._reference_now(extra)
        messages: List[Any] = []
        assistant_entries: List[tuple] = []
        consumed_steps: set = set()
        last_assistant: Optional[Any] = None
        model: Optional[str] = extra.get("model")
        last_pos = -1

        for node, role in pairs:
            node_pos = positions.get(id(node), 1 << 30)
            if role == "user":
                content = self._user_text(node)
                reasoning = ""
            else:
                content = self._assistant_text(node)
                reasoning = self._reasoning_of(node)
                # etapes de recherche rendues dans un tour distinct place avant
                # la reponse (Pro Search)
                extra_steps = []
                for spos, step in step_positions:
                    if last_pos < spos < node_pos:
                        consumed_steps.add(spos)
                        step_reasoning = self._reasoning_of(step)
                        if step_reasoning:
                            extra_steps.append(step_reasoning)
                if extra_steps:
                    reasoning = "\n\n".join([reasoning] + extra_steps).strip()
            last_pos = node_pos
            if not content and not reasoning:
                continue
            # horodatage visible (« 25 sept., 19:01 ») ; repli sur l'en-tete de
            # fil (`<time datetime>`) si le tour n'en expose pas.
            visible_ts = self._visible_timestamp(node)
            timestamp = (
                self._parse_visible_timestamp(visible_ts, reference)
                or page_timestamp
            )
            metadata: Dict[str, Any] = {}
            if role == "assistant":
                metadata["tokens"] = None
                if not model:
                    model = self._model_of(soup)
            message = self.msg(role, content, timestamp, metadata)
            if reasoning:
                message.reasoning = reasoning
            if role == "assistant":
                sources = self._sources_of(node)
                if sources:
                    message.sources = sources
                tools = self._tools_of(node)
                if tools:
                    message.tools = tools
                artifacts = self._artifacts_of(node)
                if artifacts:
                    message.artifacts = artifacts
                assistant_entries.append((message, node))
                last_assistant = message
            messages.append(message)

        if not messages:
            raise ParseError(
                "perplexity: aucun message extrait — session invalide ou DOM modifie"
            )

        # Tour de progression Pro Search resté sans réponse (dernier tour en
        # cours/échoué) : faute de message suivant, ses étapes sont rattachées au
        # dernier message assistant — la donnée serait autrement perdue.
        orphan_steps = [
            self._reasoning_of(step)
            for spos, step in step_positions
            if spos not in consumed_steps
        ]
        orphan_steps = [s for s in orphan_steps if s]
        if orphan_steps and last_assistant is not None:
            last_assistant.reasoning = "\n\n".join(
                [last_assistant.reasoning] + orphan_steps
            ).strip()

        # panneau « Sources N » de fil : cartes (titre+URL) collectees par le
        # service hors des tours, reparties sur les messages assistant.
        self._attach_thread_sources(assistant_entries, extra)

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

    @staticmethod
    def _reference_now(extra: Dict[str, Any]) -> datetime:
        """Date de reference pour deduire l'annee des horodatages partiels.

        Les etalons passent ``now`` dans ``extra`` pour des tests deterministes ;
        en production on retombe sur l'heure courante.
        """
        raw = (extra or {}).get("now") or (extra or {}).get("reference_now")
        parsed = parse_iso(raw) if raw else None
        return parsed or datetime.now(timezone.utc)

    @classmethod
    def _visible_timestamp(cls, node: Tag) -> Optional[str]:
        """Libelle d'horodatage visible d'un tour (barre d'outils / en-tete).

        Le corps markdown (``data-renderer='lm'``) est ignore : une heure
        presente dans la reponse n'est pas un horodatage de message.
        """
        for span in node.find_all("span"):
            if span.find_parent(attrs={"data-renderer": "lm"}) is not None:
                continue
            text = re.sub(r"\s+", " ", span.get_text(" ", strip=True))
            if not text or ":" not in text:
                continue
            if _TS_LABEL_RE.match(text) or _TS_RELATIVE_RE.match(text):
                return text
        return None

    @staticmethod
    def _parse_visible_timestamp(
        label: Optional[str], reference: Optional[datetime] = None
    ) -> Optional[str]:
        """Libelle Perplexity -> ISO-8601.

        Les bulles n'affichent pas l'annee (« 25 sept., 19:01 ») : on la deduit
        de la date de reference (annee courante, moins un an si la date tombe
        dans le futur). Un libelle non reconnu retourne ``None``.
        """
        if not label:
            return None
        now = reference or datetime.now(timezone.utc)
        text = re.sub(r"\s+", " ", label.strip())
        relative = _TS_RELATIVE_RE.match(text)
        if relative:
            day = now.date()
            if relative.group(1).lower().startswith("hier"):
                day = day - timedelta(days=1)
            return to_iso_z(
                datetime(
                    day.year, day.month, day.day,
                    int(relative.group(2)), int(relative.group(3)),
                    tzinfo=timezone.utc,
                )
            )
        match = _TS_LABEL_RE.match(text)
        if not match:
            return None
        day_raw, month_raw, hour_raw, minute_raw = match.groups()
        hour, minute = int(hour_raw), int(minute_raw)
        if hour > 23 or minute > 59:
            return None
        if not day_raw:
            return to_iso_z(
                now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            )
        month = _FRENCH_MONTHS.get((month_raw or "").lower().rstrip("."))
        if not month:
            return None
        candidate = datetime(
            now.year, month, int(day_raw), hour, minute, tzinfo=timezone.utc
        )
        if candidate > now + timedelta(days=2):
            try:
                candidate = candidate.replace(year=now.year - 1)
            except ValueError:
                return None
        return to_iso_z(candidate)

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
        text = cls.text_of(work)
        # 2026 : les pieces jointes sont des « chips » rendus dans le conteneur
        # de tour, hors de la bulle de texte (cf. `_attachment_chips`).
        chips = cls._attachment_chips(node)
        if chips:
            text = (text.rstrip() + "\n\n" + chips).strip()
        return text

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
                # images generees : carrousel hors du corps `lm`, a ne pas perdre
                joined = "\n\n".join(parts)
                parts.extend(
                    img for img in cls._generated_images(node) if img not in joined
                )
                return "\n\n".join(parts)
        # repli : retirer l'en-tete de workflow (« Recherche terminee ») et le
        # footer d'actions avant extraction.
        work = copy.copy(node)
        for header in work.select("[class*='step-header']"):
            header.decompose()
        for footer in work.select("[data-workflow-text-footer]"):
            footer.decompose()
        text = cls._content_text(work)
        images = [img for img in cls._generated_images(node) if img not in text]
        if images:
            text = (text.rstrip() + "\n\n" + "\n\n".join(images)).strip()
        return text

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

    #: extensions considerees comme des pieces jointes image
    _IMAGE_EXT_RE = re.compile(
        r"\.(png|jpe?g|gif|webp|bmp|svg|avif|heic|heif)$", re.IGNORECASE
    )

    @classmethod
    def _attachment_chips(cls, node: Tag) -> str:
        """Pieces jointes du tour (« chips »), rendues en markdown.

        Depuis 2026, Perplexity place les fichiers joints dans le conteneur de
        tour ``[data-workflow-entry]``, a cote de la bulle ``group/user-bubble``
        (et non plus dedans). La collecte memorisait la bulle seule : les noms de
        fichiers et les miniatures etaient perdus. On lit ici les chips du tour
        (hors de la bulle, deja couverte par ``_preserve_file_attachments``) pour
        garder ``upload_*`` / ``image_markdown`` detectables.

        Un chip image sans miniature (URL S3 expiree) est rendu en
        ``![nom](nom)`` : Perplexity n'expose alors aucune URL, mais la piece
        jointe image reste presente dans le texte.
        """
        turn = node.find_parent(attrs={"data-workflow-entry": True})
        if turn is None:
            return ""
        parts: List[str] = []
        for chip in turn.select("[data-asset-chip]"):
            if chip is node or node in chip.parents:
                continue  # deja rendu dans le texte de la bulle
            label = (
                chip.get("title")
                or chip.get("aria-label")
                or chip.get_text(" ", strip=True)
            )
            label = (label or "").strip()
            if not label:
                continue
            img = chip.find("img")
            src = (img.get("src") or "").strip() if img is not None else ""
            if src:
                parts.append(f"![{label}]({src})")
            elif cls._IMAGE_EXT_RE.search(label):
                parts.append(f"![{label}]({label})")
            else:
                parts.append(label)
        return "\n\n".join(parts)

    @classmethod
    def _generated_images(cls, node: Tag) -> List[str]:
        """Images generees rendues hors du corps ``lm`` (carrousel).

        Perplexity affiche les images produites dans un carrousel
        ``[data-testid='image-carousel-img']``, frere du corps
        ``[data-renderer='lm']`` : le corps seul les omettait. On les rend en
        markdown (telechargees localement par le pipeline).
        """
        images: List[str] = []
        for img in node.select(
            "[data-testid='image-carousel-img'] img,"
            " img[data-testid='image-carousel-img']"
        ):
            src = (img.get("src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            alt = (img.get("alt") or "image").strip() or "image"
            markdown = f"![{alt}]({src})"
            if markdown not in images:
                images.append(markdown)
        return images

    #: type affiche d'un artefact -> extension de fichier
    _ARTIFACT_EXT = {
        "yaml": "yaml", "yml": "yml", "json": "json", "markdown": "md",
        "md": "md", "html": "html", "csv": "csv", "python": "py",
        "javascript": "js", "typescript": "ts", "text": "txt", "xml": "xml",
        "sql": "sql", "shell": "sh", "bash": "sh",
    }

    @classmethod
    def _artifacts_of(cls, node: Tag) -> List[str]:
        """Artefacts/canvas produits dans un tour assistant.

        Perplexity rend une carte d'artefact (titre + type, bouton
        « Options de l'artefact ») hors du corps ``lm``. On liste le nom de
        fichier reconstruit (``titre`` + extension deduite du type quand il est
        connu : ``notes`` + ``YAML`` -> ``notes.yaml``).
        """
        artifacts: List[str] = []
        for opts in node.find_all("button"):
            aria = (opts.get("aria-label") or "").lower()
            if "artefact" not in aria and "artifact" not in aria:
                continue
            row = opts
            while row is not None and not (
                row.name == "div"
                and row.get("class")
                and "group" in row.get("class")
                and "relative" in row.get("class")
            ):
                row = row.parent
            scope = row if row is not None else node
            name_el = scope.find("div", class_="font-bold")
            name = name_el.get_text(" ", strip=True) if name_el is not None else ""
            type_el = scope.find("div", class_="text-secondary")
            kind = type_el.get_text(" ", strip=True) if type_el is not None else ""
            label = name
            ext = cls._ARTIFACT_EXT.get(kind.lower())
            if name and ext and not name.lower().endswith("." + ext):
                label = f"{name}.{ext}"
            if label and label not in artifacts:
                artifacts.append(label)
        return artifacts

    @classmethod
    def _reasoning_of(cls, node: Tag) -> str:
        """Etapes de recherche/raisonnement d'un tour (si exposees).

        La progression de recherche est rendue dans des en-tetes
        ``group/step-header`` (hors corps ``lm``). Le libelle de statut
        (« Recherche terminee ») n'est pas du raisonnement : on ne garde que les
        etapes nommees (ex. « Verification des fichiers disponibles »). Sur les
        etalons 2026 (modele ``turbo``), les etapes nommees n'apparaissent que
        dans un tour de progression separe (traite par `parse`) : ce champ reste
        vide pour le corps de reponse, mais se remplit des que le DOM les y
        expose.
        """
        steps: List[str] = []
        for header in node.select("[class*='step-header']"):
            for el in header.select("[title]"):
                label = (el.get("title") or "").strip()
                if not label or label.lower().startswith("recherche termin"):
                    continue
                if label not in steps:
                    steps.append(label)
        return "\n\n".join(steps)

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
        """Sources/citations web d'un tour assistant.

        Deux formes cohabitent dans le DOM 2026 :
          - citations inline ``[data-pplx-citation-url]`` : l'URL complete est
            portee par l'attribut et le titre eventuel par ``aria-label`` ;
          - cartes de sources en bas de reponse (liens vers les articles).
        Chaque source est rendue en « titre — url » quand un titre est expose,
        sinon en URL seule. Deduplication par URL, ordre du DOM.
        """
        root = answer_node.parent if answer_node.parent is not None else answer_node
        cards: Dict[int, Tag] = {}
        for sel in cls.SOURCE_SELECTORS:
            try:
                for el in root.select(sel):
                    cards[id(el)] = el
            except Exception:  # noqa: BLE001 - selecteur invalide ignore
                continue
        sources: List[str] = []
        seen = set()
        for el in root.find_all(True):
            if el.get("data-pplx-citation-url"):
                url = el.get("data-pplx-citation-url")
                title = cls._citation_title(el)
            else:
                card = cards.get(id(el))
                if card is None or card.name != "a":
                    continue
                url = card.get("href")
                title = (card.get("title") or card.get("aria-label") or "").strip()
                if not title:
                    text = card.get_text(" ", strip=True)
                    if cls._looks_like_source_title(text):
                        title = text
            url = (url or "").strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            sources.append(f"{title} — {url}" if title else url)
            if len(sources) >= 40:
                break
        return sources

    @staticmethod
    def _citation_title(el: Tag) -> str:
        """Titre d'une citation inline (``aria-label`` du noeud interne).

        L'URL et le titre ne sont pas sur le meme noeud : ``span.citation``
        porte ``data-pplx-citation-url`` et un descendant porte le titre dans
        son ``aria-label`` (a ignorer les libelles d'interface courts).
        """
        own = (el.get("aria-label") or "").strip()
        if own:
            return own
        for child in el.find_all(attrs={"aria-label": True}):
            label = (child.get("aria-label") or "").strip()
            if len(label) >= 12 and not label.lower().startswith("domaine fiable"):
                return label
        return ""

    @staticmethod
    def _looks_like_source_title(text: Optional[str]) -> bool:
        """Vrai si le texte d'un lien peut etre un titre d'article (pas un domaine).

        Un intitule de source fait au moins trois mots (« Le Monde — Article ») ;
        les libelles du type « 2 eea.europa.eu » ou une URL sont ecartes.
        """
        text = (text or "").strip()
        if len(text) < 20 or text.startswith(("http://", "https://")):
            return False
        if _TS_LABEL_RE.match(text) or _TS_RELATIVE_RE.match(text):
            return False
        return len(text.split()) >= 3

    #: source annoncee dans le pied d'un tour (« 10 sources », « 1 source »)
    _SOURCE_COUNT_RE = re.compile(r"(\d+)\s*sources?", re.IGNORECASE)

    @staticmethod
    def _source_url(value: Optional[str]) -> Optional[str]:
        """URL contenue dans une entree de source (« titre — url » ou url seule)."""
        text = (value or "").strip()
        if text.startswith(("http://", "https://")):
            return text
        if " — " in text:
            tail = text.rsplit(" — ", 1)[1].strip()
            if tail.startswith(("http://", "https://")):
                return tail
        return None

    @classmethod
    def _append_source(cls, message, title: str, url: str) -> None:
        """Ajoute une source au message, dedupliquee par URL.

        Sans titre, l'URL seule est ecrite ; un libelle « url » deja present est
        enrichi du titre quand le panneau en expose un.
        """
        label = f"{title} — {url}" if title else url
        for index, existing in enumerate(message.sources):
            if cls._source_url(existing) == url:
                if title and " — " not in existing:
                    message.sources[index] = label
                return
        message.sources.append(label)

    @staticmethod
    def _thread_sources(extra: Optional[Dict[str, Any]]) -> List[tuple]:
        """Cartes du panneau « Sources N » de fil (titre, URL).

        Le service ouvre le panneau pendant la capture et passe les cartes dans
        ``extra["thread_sources"]`` (liste ou dict ``{count, sources}``).
        """
        raw: Any = (extra or {}).get("thread_sources") or []
        if isinstance(raw, dict):
            raw = raw.get("sources") or []
        out: List[tuple] = []
        seen = set()
        for item in raw:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "").strip()
            else:
                url = str(item or "").strip()
                title = ""
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            out.append((title, url))
            if len(out) >= 40:
                break
        return out

    @classmethod
    def _web_source_count(cls, node: Tag) -> Optional[int]:
        """Nombre de sources web annonce par le pied d'un tour assistant.

        Perplexity affiche « N sources » dans un bouton avec les favicons des
        domaines ; les pieces jointes utilisent le meme libelle mais une icone
        fichier/image (pas de favicon). Sert a attribuer le panneau de fil quand
        les citations inline manquent.
        """
        best: Optional[int] = None
        for button in node.find_all("button"):
            if button.find("img", src=re.compile(r"favicons")) is None:
                continue
            match = cls._SOURCE_COUNT_RE.search(button.get_text(" ", strip=True))
            if not match:
                continue
            count = int(match.group(1))
            best = count if best is None else max(best, count)
        return best

    @classmethod
    def _attach_thread_sources(cls, entries: List[tuple], extra: Optional[dict]) -> None:
        """Repartit les cartes du panneau Sources de fil sur les messages assistant.

        Le panneau est agrege au fil : il ne dit pas a quelle reponse appartient
        chaque carte. On attribue par URL via les citations inline de chaque
        reponse ; a defaut, au message dont le compteur « N sources » egale le
        nombre de cartes ; sinon au dernier message assistant (la donnee serait
        autrement perdue). Ce choix est documente ici.
        """
        thread = cls._thread_sources(extra)
        if not thread or not entries:
            return
        inline_targets = [i for i, (msg, _node) in enumerate(entries) if msg.sources]
        if inline_targets:
            last = inline_targets[-1]
            for title, url in thread:
                target = last
                for index in inline_targets:
                    urls = {
                        cls._source_url(s) for s in entries[index][0].sources
                    }
                    if url in urls:
                        target = index
                        break
                cls._append_source(entries[target][0], title, url)
            return
        counts = [cls._web_source_count(node) for _msg, node in entries]
        matches = [
            index
            for index, count in enumerate(counts)
            if count is not None and count == len(thread)
        ]
        target_index = matches[-1] if matches else len(entries) - 1
        for title, url in thread:
            cls._append_source(entries[target_index][0], title, url)

    #: en-tetes de workflow -> outil utilise (libelle standardise)
    _TOOL_KEYWORDS = (
        (re.compile(r"recherche|search", re.IGNORECASE), "Recherche"),
        (re.compile(r"\bcomputer\b", re.IGNORECASE), "Computer"),
        (re.compile(r"ex[ée]cution|code", re.IGNORECASE), "Code"),
    )

    @classmethod
    def _tools_of(cls, node: Tag) -> List[str]:
        """Outils/etapes visibles d'un tour (en-tetes de workflow).

        Perplexity affiche « Recherche terminée » dans un en-tete
        ``group/step-header`` : c'est le seul indicateur d'outil expose dans le
        DOM. Les libelles connus sont normalises (``Recherche``, ``Computer``,
        ``Code``) et dedupliques.
        """
        tools: List[str] = []
        for header in node.select("[class*='step-header']"):
            text = header.get_text(" ", strip=True)
            for pattern, label in cls._TOOL_KEYWORDS:
                if pattern.search(text) and label not in tools:
                    tools.append(label)
        return tools

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
