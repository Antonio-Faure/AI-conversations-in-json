"""Parser DOM Mistral (chat.mistral.ai).

Structure observee (2026) :
  - tour de message : <div data-message-author-role="user|assistant"
                           data-message-id="..." data-message-version="0">
  - user      : <div class="select-text"><span class="whitespace-pre-wrap">...</span></div>
  - assistant : parties <div data-message-part-type="answer"> (texte de la
                reponse) et <div data-message-part-type="reasoning"> (bloc
                « Reflechi » / Magistral, extrait dans `Message.reasoning`) ;
                le texte est dans .markdown-container-style
  - pieces jointes : vignettes/images dans la grille du tour ; les noms/types/
                URL complets sont aussi presents dans le payload RSC Next.js
                (`self.__next_f.push`) sous forme de `"files"` par message.
  - canvas/artefacts : carte inline `[data-review-comment-boundary="canvas"]`
                (titre dans l'entete, type dans `data-review-comment-canva-type`)
                -> `Message.artifacts`, ex. "Rapport (text/markdown)".
  - horodatage : visible `div.text-hint` (« 24 sept., 14:27 ») ; repli sur
                `createdAt` du payload RSC -> `Message.timestamp` (ISO).
  - modele    : `chat.modelConfig.model_alias` du payload RSC (nom affiche
                resolu via `model_display_name`) -> `Conversation.model`.
  - sources   : citations web (`references`, resultats de `web_search`) ;
                repli sur les cartes « N sources » du DOM -> `Message.sources`.
  - outils    : `tool_call`/`canva` du payload RSC (recherche, code, canvas,
                connecteurs...) -> `Message.tools`.
  - titre : <title> de la page (ex: "Estimation tokens ...")
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import NavigableString

from ..schema import Conversation, parse_iso, to_iso_z
from .base import BaseParser, ParseError

#{ deux modes : /chat (chatbot) et /work (agentique). Les projets
# (/chat/projects/<id>) ne sont pas des conversations et sont exclus par le
# motif d'id (uuid juste apres /chat/ ou /work/).
ID_RE = re.compile(r"/(?:chat|work)/([0-9a-fA-F-]{8,})")

#: origine de l'app (les images generees sont referencees en chemin relatif
#: `/cdn-cgi/image/...` : il faut les absolutiser avant telechargement)
BASE_URL = "https://chat.mistral.ai"

# -- horodatages visibles (« 24 sept., 14:27 », « Hier 12:05 », « 2:04 ») ------
_TIMESTAMP_SELECTORS = (
    "div[class*='text-hint'][class*='text-sm']",
    "div.text-hint",
)
_TS_TIME_ONLY_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_TS_RELATIVE_RE = re.compile(r"^(aujourd'?hui|hier)\s+(\d{1,2}):(\d{2})$", re.I)
_TS_DAY_RE = re.compile(r"^(\d{1,2})\s+([a-zéûôà]+)\.?,?\s+(\d{1,2}):(\d{2})$", re.I)
_TS_DAY_YEAR_RE = re.compile(
    r"^(\d{1,2})\s+([a-zéûôà]+)\.?\s+(\d{4}),?\s+(\d{1,2}):(\d{2})$", re.I
)
_FRENCH_MONTHS = {
    "janv": 1, "janvier": 1, "févr": 2, "fevr": 2, "février": 2, "fevrier": 2,
    "mars": 3, "avr": 4, "avril": 4, "mai": 5, "juin": 6, "juil": 7,
    "juillet": 7, "août": 8, "aout": 8, "sept": 9, "septembre": 9,
    "oct": 10, "octobre": 10, "nov": 11, "novembre": 11, "déc": 12,
    "decembre": 12, "décembre": 12,
}

#: outils Mistral (nom du tool_call) -> libelle standardise
_TOOL_LABELS = {
    "web_search": "Recherche web",
    "open_search_results": "Résultats de recherche",
    "open_url": "Ouverture de page web",
    "search_tool_functions": "Recherche d'outil",
    "code_interpreter": "Exécution de code",
    "run_typescript": "Exécution de code",
    "bash": "Exécution de code",
    "generate_image": "Génération d'image",
    "image_generation": "Génération d'image",
    "analyze_image": "Analyse d'image",
    "write_canvas": "Canvas",
    "read_file": "Lecture de fichier",
    "write_file": "Écriture de fichier",
    "search_replace": "Modification de fichier",
    "skill": "Compétence",
    "read_resource": "Connecteur",
    "library_search": "Bibliothèque",
    "library_list": "Bibliothèque",
}
#: prefixe de nom d'outil -> libelle (connecteurs)
_TOOL_PREFIXES = (
    ("gmail", "Gmail"),
    ("github", "GitHub"),
    ("google_calendar", "Google Agenda"),
    ("library", "Bibliothèque"),
)
#: etapes visibles en repli (HTML sans payload RSC)
_DOM_TOOL_MARKERS = (
    (re.compile(r"recherch[ée] sur le web", re.I), "Recherche web"),
    (re.compile(r"ouvert une page", re.I), "Ouverture de page web"),
    (re.compile(r"ex[ée]cut[ée]", re.I), "Exécution de code"),
    (re.compile(r"g[ée]n[ée]r[ée] une image", re.I), "Génération d'image"),
    (re.compile(r"modifi[ée] un canvas", re.I), "Canvas"),
)
#: nombre maximum de sources conservees par message
SOURCE_LIMIT = 40


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
        payload = self._rsc_payload(html)
        message_data = self._message_index(payload)
        conversation = self._conversation_payload(payload)
        model = self._model_of(payload, conversation)
        reference = self._reference_now(extra)

        messages: List[Any] = []
        for container in soup.select("[data-message-author-role]"):
            role = (container.get("data-message-author-role") or "").lower()
            if role not in ("user", "assistant", "system", "tool"):
                role = "assistant"
            message_id = str(container.get("data-message-id") or "")
            data = message_data.get(message_id) or {}
            files = data.get("files") if isinstance(data.get("files"), list) else None
            text = self._text_of_turn(container, role, files)
            reasoning = self._reasoning_of_turn(container) if role == "assistant" else ""
            artifacts = self._artifacts_of_turn(container) if role == "assistant" else []
            if not text and not reasoning and not artifacts:
                continue
            timestamp = self._timestamp_of(data, container, reference)
            message = self.msg(
                role,
                text,
                timestamp,
                {"tokens": None} if role == "assistant" else {},
                message_id=message_id,
            )
            if reasoning:
                message.reasoning = reasoning
            if artifacts:
                message.artifacts = artifacts
            if role == "assistant":
                tools = self._tools_of(data, container)
                if tools:
                    message.tools = tools
                sources = self._sources_of(data, container)
                if sources:
                    message.sources = sources
            messages.append(message)

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
            model=model or None,
        )
        self.log_parse(conv, title=title, model=model)
        return self.check(conv)

    # -- payload RSC (horodatages, modele, sources, outils) --------------------

    @classmethod
    def _rsc_payload(cls, html: str) -> str:
        """Concatene les fragments du payload RSC Next.js (`self.__next_f`)."""
        if "__next_f" not in html:
            return ""
        parts: List[str] = []
        for match in _RSC_PUSH_RE.finditer(html):
            try:
                parts.append(json.loads(match.group(1)))
            except ValueError:
                continue
        return "".join(parts)

    @classmethod
    def _message_index(cls, payload: str) -> Dict[str, Dict[str, Any]]:
        """Index `message_id -> objet message` du payload RSC.

        Le payload contient, pour chaque tour, l'objet complet (role, contenu,
        `createdAt`, `files`, `references`, `contentChunks` dont les outils) :
        c'est la source structuree des metadonnees. En cas de versions
        multiples, la premiere occurrence (la plus recente) est gardee.
        """
        index: Dict[str, Dict[str, Any]] = {}
        if not payload:
            return index
        for match in _MSG_START_RE.finditer(payload):
            raw = cls._json_object_at(payload, match.start())
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            message_id = data.get("id")
            if message_id and str(message_id) not in index:
                index[str(message_id)] = data
        return index

    @classmethod
    def _conversation_payload(cls, payload: str) -> Dict[str, Any]:
        """Objet conversation (`chat`) du payload RSC (modele, productType)."""
        if not payload:
            return {}
        for match in re.finditer(r'\{\s*"chat"\s*:\s*\{', payload):
            raw = cls._json_object_at(payload, match.start())
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            chat = data.get("chat")
            if isinstance(chat, dict) and ("productType" in chat or "modelConfig" in chat):
                return chat
        return {}

    @classmethod
    def _model_display_map(cls, payload: str) -> Dict[str, str]:
        """Alias de modele -> nom affiche (`vibeWorkModelConfigs`)."""
        mapping: Dict[str, str] = {}
        for match in re.finditer(r'"model_alias"\s*:\s*"([^"]+)"', payload):
            tail = payload[match.end() : match.end() + 400]
            display = re.search(r'"model_display_name"\s*:\s*"([^"]+)"', tail)
            if display:
                mapping.setdefault(match.group(1), display.group(1))
        return mapping

    @classmethod
    def _model_of(cls, payload: str, conversation: Dict[str, Any]) -> Optional[str]:
        """Modele de la conversation (nom affiche, repli alias puis defaut page).

        En mode work, `modelConfig.model_alias` porte le modele choisi et
        `model_display_name` son nom affiche ; en mode chat la conversation n'a
        pas de modele propre -> on retombe sur le modele gere de la page, puis
        sur `jarvis_model.name`.
        """
        alias = ""
        config = (conversation or {}).get("modelConfig")
        if isinstance(config, dict):
            alias = str(config.get("model_alias") or "").strip()
        display_map = cls._model_display_map(payload)
        if alias:
            return display_map.get(alias, alias)
        for display in display_map.values():
            if display:
                return display
        jarvis = re.search(r'"jarvis_model"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', payload)
        return jarvis.group(1) if jarvis else None

    # -- horodatages -----------------------------------------------------------

    @staticmethod
    def _reference_now(extra: Dict[str, Any]) -> datetime:
        """Date de reference pour deduire l'annee des horodatages partiels.

        Les tests passent ``now`` dans ``extra`` pour etre deterministes ; en
        production on retombe sur l'heure courante.
        """
        raw = (extra or {}).get("now") or (extra or {}).get("reference_now")
        parsed = parse_iso(raw) if raw else None
        return parsed or datetime.now(timezone.utc)

    @classmethod
    def _timestamp_of(cls, data: Dict[str, Any], container, reference: datetime) -> Optional[str]:
        """Horodatage d'un tour : libelle visible, repli `createdAt` RSC."""
        return cls._visible_timestamp(container, reference) or cls._rsc_timestamp(data)

    @staticmethod
    def _rsc_timestamp(data: Dict[str, Any]) -> Optional[str]:
        value = data.get("createdAt") if isinstance(data, dict) else None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text.startswith("$D"):  # marqueur de date RSC
            text = text[2:]
        return text or None

    @classmethod
    def _visible_timestamp(cls, container, reference: datetime) -> Optional[str]:
        """Libelle d'horodatage visible -> ISO (None si non reconnu)."""
        label = ""
        for sel in _TIMESTAMP_SELECTORS:
            for el in container.select(sel):
                text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
                if text and ":" in text:
                    label = text
                    break
            if label:
                break
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
        full = _TS_DAY_YEAR_RE.match(text)
        if full:
            day, month_raw, year, hour_raw, minute_raw = full.groups()
            month = _FRENCH_MONTHS.get(month_raw.lower().rstrip("."))
            if not month:
                return None
            return to_iso_z(
                datetime(
                    int(year), month, int(day),
                    int(hour_raw), int(minute_raw),
                    tzinfo=timezone.utc,
                )
            )
        partial = _TS_DAY_RE.match(text)
        if partial:
            day_raw, month_raw, hour_raw, minute_raw = partial.groups()
            hour, minute = int(hour_raw), int(minute_raw)
            if hour > 23 or minute > 59:
                return None
            month = _FRENCH_MONTHS.get(month_raw.lower().rstrip("."))
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
        time_only = _TS_TIME_ONLY_RE.match(text)
        if time_only:
            hour, minute = int(time_only.group(1)), int(time_only.group(2))
            if hour > 23 or minute > 59:
                return None
            return to_iso_z(
                now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            )
        return None

    # -- sources / outils ------------------------------------------------------

    @classmethod
    def _sources_of(cls, data: Dict[str, Any], container) -> List[str]:
        """Citations web d'un tour assistant (« titre — url », dedupliquees).

        Trois formes dans le payload RSC : `references` (citations riches),
        resultats des tool_calls `web_search`/`open_search_results` et
        `citedSources`. En l'absence de payload (HTML recompose), repli sur les
        cartes « N sources » du DOM.
        """
        sources: List[str] = []
        seen: set = set()

        def add(url: Any, title: Any = "") -> None:
            url = str(url or "").strip()
            title = re.sub(r"\s+", " ", str(title or "")).strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                return
            seen.add(url)
            if title and title != url and title not in url:
                sources.append(f"{title} — {url}")
            else:
                sources.append(title or url)

        for ref in data.get("references") or []:
            if isinstance(ref, dict):
                add(ref.get("url"), ref.get("title"))
        for chunk in data.get("contentChunks") or []:
            if not isinstance(chunk, dict) or chunk.get("type") != "tool_call":
                continue
            if chunk.get("name") not in ("web_search", "open_search_results", "open_url"):
                continue
            result = chunk.get("publicResult")
            if isinstance(result, dict):
                for entry in result.get("results") or []:
                    if isinstance(entry, dict):
                        add(entry.get("url"), entry.get("title"))
        for citation in data.get("citedSources") or []:
            if isinstance(citation, dict):
                add(citation.get("url"), citation.get("title"))
            elif isinstance(citation, str):
                add(citation)
        if not sources:
            sources = cls._dom_sources(container)
        return sources[:SOURCE_LIMIT]

    @classmethod
    def _dom_sources(cls, container) -> List[str]:
        """Cartes de sources du DOM (repli sans payload) : liens de la carte."""
        if container is None:
            return []
        sources: List[str] = []
        seen: set = set()
        for span in container.find_all("span"):
            if not re.match(r"^\d+\s+sources?$", span.get_text(" ", strip=True), re.I):
                continue
            card = span.find_parent("div", class_="bg-card")
            if card is None:
                continue
            for anchor in card.find_all("a", href=True):
                url = (anchor.get("href") or "").strip()
                if not url.startswith(("http://", "https://")) or url in seen:
                    continue
                seen.add(url)
                title = anchor.get_text(" ", strip=True)
                sources.append(
                    f"{title} — {url}" if title and title not in url else (title or url)
                )
        return sources[:SOURCE_LIMIT]

    @classmethod
    def _tools_of(cls, data: Dict[str, Any], container) -> List[str]:
        """Outils/etapes d'un tour : `tool_call` et `canva` du payload RSC.

        Le nom du tool_call est normalise (recherche, code, canvas, connecteur)
        et deduplique ; en l'absence de payload, repli sur les libelles d'etape
        visibles du DOM.
        """
        tools: List[str] = []
        seen: set = set()
        for chunk in data.get("contentChunks") or []:
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "tool_call":
                label = cls._tool_label(chunk.get("name"), chunk)
            elif chunk.get("type") == "canva":
                label = "Canvas"
            else:
                continue
            if label and label not in seen:
                seen.add(label)
                tools.append(label)
        if not tools:
            tools = cls._dom_tools(container)
        return tools

    @classmethod
    def _tool_label(cls, name: Any, chunk: Optional[Dict[str, Any]] = None) -> str:
        """Libelle standardise d'un outil depuis son nom et ses metadonnees."""
        text = str(name or "").strip()
        if not text:
            return ""
        display = ((chunk or {}).get("publicMetadata") or {}).get("display") or {}
        kind = str(display.get("kind") or "")
        if "canvas" in kind.lower() or "canvas" in text.lower():
            return "Canvas"
        if text in _TOOL_LABELS:
            return _TOOL_LABELS[text]
        low = text.lower()
        for prefix, label in _TOOL_PREFIXES:
            if low.startswith(prefix):
                return label
        # nom non identifiable (artefact de payload : phrase, balise...)
        if len(text) > 60 or not re.fullmatch(r"[A-Za-z0-9_.\-]+", text):
            return ""
        return text

    @classmethod
    def _dom_tools(cls, container) -> List[str]:
        """Etapes visibles du DOM (« Recherché sur le web »...), sans RSC."""
        if container is None:
            return []
        tools: List[str] = []
        for el in container.select("span.min-w-0.truncate, div.min-h-5"):
            text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            if not text or text.lower().startswith(("réfléchi", "reflechi")):
                continue
            for pattern, label in _DOM_TOOL_MARKERS:
                if pattern.search(text) and label not in tools:
                    tools.append(label)
                    break
        return tools

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
    def _reasoning_of_turn(cls, container) -> str:
        """Raisonnement (« Reflechi » / Magistral) d'un tour assistant.

        Le bloc est replie mais son markdown reste dans le DOM sous
        ``[data-message-part-type='reasoning']`` (a ne pas confondre avec la
        partie ``answer``). Les tours peuvent en contenir plusieurs : on les
        concatene dans l'ordre.
        """
        texts: List[str] = []
        for part in container.select("[data-message-part-type='reasoning']"):
            text = cls._content_text(part).strip()
            if text:
                texts.append(text)
        return "\n\n".join(texts)

    @classmethod
    def _artifacts_of_turn(cls, container) -> List[str]:
        """Canvas/artefacts d'un tour assistant, sous la forme ``titre (type)``.

        Mistral rend un canvas comme une carte inline
        ``[data-review-comment-boundary='canvas']`` (une par version) : le type
        est dans ``data-review-comment-canva-type`` et le titre dans l'entete de
        la carte. Les versions successives partagent le meme titre -> dedup.
        """
        artifacts: List[str] = []
        for canvas in container.select("[data-review-comment-boundary='canvas']"):
            media_type = (canvas.get("data-review-comment-canva-type") or "").strip()
            title = cls._canvas_title(canvas)
            if title and media_type:
                label = f"{title} ({media_type})"
            else:
                label = title or media_type
            if label and label not in artifacts:
                artifacts.append(label)
        return artifacts

    @classmethod
    def _canvas_title(cls, canvas) -> str:
        """Titre affiche d'une carte canvas (``span.truncate`` de l'entete).

        On remonte les ancetres jusqu'a la carte : le premier ``span.truncate``
        rencontre est celui de l'entete du canvas (et non le libelle du bloc de
        raisonnement, situe plus haut dans le tour).
        """
        node = canvas
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            span = node.select_one("span.truncate")
            if span is not None:
                title = span.get_text(" ", strip=True)
                if title and not title.lower().startswith(("reflechi", "réfléchi")):
                    return title
        return ""

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
