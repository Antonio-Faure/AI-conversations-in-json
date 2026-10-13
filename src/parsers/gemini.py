"""Parser DOM Gemini (gemini.google.com/app).

Structure reelle observee (2024-2026, app Angular) :
  - sidebar : element <conversation-history> avec <a href="/app/<id>">
  - message user : <user-query> (contenu dans .user-query-content .content,
    ou <message-content> imbrique, ou rich-textarea .ql-editor)
  - reponse assistant : <model-response> <message-content class="model-response-text">
  - timestamps : rarement exposes ; extraits de <time datetime> / data-timestamp
    quand presents (sinon None, l'ordre du fil fait foi)
  - modele : label de reponse « Answered by Gemini 2.x » (pied de reponse,
    data-test-id='response-footer') ou libelle d'accessibilite
    « Gemini a dit » (screen-reader-model-response-label)
  - sources : puces de citation <source-inline-chip> (grounding web / fichiers
    joints) et cartes eventuelles des <sources-list>
  - outils : marqueurs structurels (execution de code, generation d'image,
    canvas, analyse video) -> Message.tools
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString

from ..schema import Conversation, normalize_timestamp
from .base import BaseParser, ParseError, clean_ui_title

#: segments de route sous /app/ qui ne sont pas des identifiants de conversation
#: (ex. /app/download = page de telechargement de l'application). Le selecteur
#: large ``a[href*='/app/']`` les attrape : il faut les exclure de la decouverte.
RESERVED_APP_SEGMENTS = ("download",)
# IDs Gemini observes : 16 caracteres hexa (/app/892fb59332022e2a)
CONV_ID_RE = re.compile(
    r"/app/(?!(?:" + "|".join(RESERVED_APP_SEGMENTS) + r")(?![a-zA-Z0-9_-]))"
    r"([a-zA-Z0-9_-]{8,})"
)
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

#: puce de citation d'une reponse (grounding web ou fichier joint)
SOURCE_CHIP_SELECTOR = "source-inline-chip"
#: conteneurs de la liste « Sources » (cartes eventuelles)
SOURCE_LIST_SELECTORS = ("sources-list", "sources-carousel")
#: detail d'une puce dans son aria-label (« citation de X », « citations de X »)
_CITATION_ARIA_RE = re.compile(
    r"citation(?:s)?\s+de\s+(?P<detail>.+?)(?:\.\s*Appuyer\b|$)", re.IGNORECASE
)

#: horodatage eventuel d'un tour (le DOM Gemini l'expose rarement)
TIMESTAMP_SELECTORS = (
    "time[datetime]",
    "[data-timestamp]",
    "[data-utc]",
    ".response-footer time",
    ".response-container-footer time",
    "[class*='timestamp']",
    "time",
)

#: outil/etape deduit d'un marqueur structurel de la reponse
TOOL_MARKERS = (
    (
        (".code-result-container", "[data-test-id='code-output-stdout-stderr']"),
        "Exécution de code",
    ),
    ((".attachment-container.generated-images", "generated-image"), "Génération d'images"),
    (("svg-code-block", "immersive-editor"), "Canvas"),
    (
        (".luminous-video-duration-pill", "[data-test-id='video-thumbnail']"),
        "Analyse vidéo",
    ),
)


#: noeuds d'accessibilite qui dupliquent la requete dans un tour virtualise
_UI_TEXT_SELECTORS = (
    ".cdk-visually-hidden",
    "[class*='screen-reader']",
    "[aria-hidden='true']",
)

# -- mapping API (payload brut `hNvQHb` de batchexecute) ----------------------
#
# Le payload de conversation est un tableau de tours :
#   tour = [ [conv_id, r_<user>], [conv_id, r_<suite>, rc_<reponse>],
#            <contenu utilisateur>, <contenu assistant>, [epoch, nanos] ]
# - contenu utilisateur : parts `[texte, .., [groupes de pieces jointes]]`
# - contenu assistant : candidats `[rc_id, [texte markdown], .., champs]` ;
#   le texte porte des annotations (`?code_reference`, `[cite: N]`,
#   `[file-tag: ..]`, URL d'image generee) qu'on normalise vers le markdown du
#   schema, et les champs structures (dicts protobuf : `44` citations, `59`
#   fichiers generes) portent les sources et les liens de telechargement. Les
#   positions des champs varient selon la requete : on parcourt la structure au
#   lieu de lire des index fixes.

#: annotation de fence du payload : ```python?code_reference&code_event_index=1
API_FENCE_RE = re.compile(r"(?m)^```([^\n`?]*)\?[^\n`]*$")
#: bloc de code complet (info + corps) pour normaliser les lignes vides finales
API_FENCE_BLOCK_RE = re.compile(r"(?ms)^```([^\n`]*)\n(.*?)^```[ \t]*$")
#: marqueur de citation `[cite: N]` (la source est dans le champ `44`)
API_CITE_RE = re.compile(r"\[cite:\s*\d+\]")
#: marqueur de fichier genere `[file-tag: <id>]` (le lien est dans le champ `59`)
API_FILE_TAG_RE = re.compile(r"\[file-tag:\s*[^\]]+\]")
#: URL interne d'une image generee (remplacee par le markdown de l'image reelle)
API_GENERATED_IMAGE_RE = re.compile(
    r"https?://googleusercontent\.com/image_generation_content/\S+"
)
#: puce markdown du payload (`* item`) -> convention du rendu DOM (`- item`)
API_BULLET_RE = re.compile(r"(?m)^(\s*)[*+] ")
#: nom d'outil API -> libelle du schema (memes libelles que le parseur DOM)
API_TOOL_IMAGE_MARKERS = ("image", "imagen", "banana")
API_TOOL_LABELS = {
    "google": "Recherche web",
    "data_analysis_tool": "Exécution de code",
    "image_generation_tool": "Génération d'images",
    "canvas_tool": "Canvas",
    "video_tool": "Analyse vidéo",
}


def _api_parts(content: Any) -> List[list]:
    """Parts d'un message utilisateur (le champ ``turn[2]``).

    Gemini envoie soit une part unique ``[texte, ..]``, soit une liste de
    parts ; on normalise vers une liste de parts.
    """
    if not isinstance(content, list) or not content:
        return []
    first = content[0]
    if isinstance(first, str):
        return [content]
    if isinstance(first, list):
        if first and isinstance(first[0], list):
            return [part for part in first if isinstance(part, list)]
        return [first]
    return []


def _api_part_text(part: Any) -> str:
    """Texte d'une part utilisateur (premier element)."""
    return part[0] if isinstance(part, list) and isinstance(part[0], str) else ""


def _api_walk(node: Any):
    """Parcourt recursivement listes et dictionnaires d'une structure API."""
    if isinstance(node, list):
        yield node
        for item in node:
            yield from _api_walk(item)
    elif isinstance(node, dict):
        yield node
        for item in node.values():
            yield from _api_walk(item)


def _api_values(node: Any) -> List[Any]:
    """Valeurs d'un noeud (elements d'une liste, valeurs d'un dictionnaire)."""
    if isinstance(node, list):
        return list(node)
    if isinstance(node, dict):
        return list(node.values())
    return []


def _api_find_attachments(node: Any) -> List[list]:
    """Noeuds pieces jointes ``[null, <type>, <nom>, ..]`` d'une structure."""
    found: List[list] = []
    if isinstance(node, list):
        if (
            len(node) >= 4
            and node[0] is None
            and isinstance(node[1], int)
            and isinstance(node[2], str)
            and node[2]
        ):
            found.append(node)
            return found
        for item in node:
            found.extend(_api_find_attachments(item))
    elif isinstance(node, dict):
        for item in node.values():
            found.extend(_api_find_attachments(item))
    return found


def _api_attachment_name(node: list) -> str:
    return str(node[2]) if len(node) > 2 and isinstance(node[2], str) else ""


def _api_attachment_mime(node: list) -> str:
    for value in node[3:]:
        if isinstance(value, str) and "/" in value and len(value) < 64:
            return value
    return ""


def _api_attachment_urls(node: list) -> List[str]:
    """URLs annexes d'une piece jointe (vignette, telechargement, envoi)."""
    urls = node[7] if len(node) > 7 and isinstance(node[7], list) else []
    return [url for url in urls if isinstance(url, str) and url.startswith("http")]


def _api_download_url(node: list) -> str:
    """URL de telechargement direct d'une piece jointe (sinon chaine vide)."""
    for url in _api_attachment_urls(node):
        if "contribution.usercontent.google.com/download" in url:
            return url
    return ""


def _api_attachment_markdown(node: list) -> str:
    """Piece jointe -> markdown : image ``![nom](url)`` ou lien de fichier.

    Les images pointent l'URL ``lh3.googleusercontent.com/gg/..`` (telechargee
    par la session) ; les fichiers non-image pointent l'URL de telechargement
    ``contribution.usercontent.google.com/download`` (nom + extension connus,
    telecharges dans ``files/``). Les medias (audio/video) portent en plus une
    vignette ``gg`` : on la rend comme l'apercu image du DOM.
    """
    name = _api_attachment_name(node)
    mime = _api_attachment_mime(node)
    if mime.startswith("image/"):
        url = node[3] if len(node) > 3 and isinstance(node[3], str) else ""
        if url.startswith("http"):
            alt = name or "image"
            return f"![{alt}]({url})"
        return ""
    blocks: List[str] = []
    thumb = next(
        (url for url in _api_attachment_urls(node) if "googleusercontent.com/gg/" in url),
        "",
    )
    if thumb and name:
        blocks.append(f"![{name}]({thumb})")
    download = _api_download_url(node)
    if name and download:
        blocks.append(f"[{name}]({download})")
    if blocks:
        return "\n\n".join(blocks)
    return name


def _api_message_attachments(part: list) -> List[list]:
    """Pieces jointes reellement attachees au message (``part[4][0][3]``).

    Le champ ``part[4][0][4]`` liste tous les fichiers de l'espace de travail
    (rappeles a chaque tour) : on ne garde que ``[3]``, l'attachement du tour.
    """
    groups = part[4] if len(part) > 4 and isinstance(part[4], list) else []
    found: List[list] = []
    for group in groups:
        if not isinstance(group, list) or len(group) < 4:
            continue
        used = group[3]
        if isinstance(used, list):
            for item in used:
                found.extend(_api_find_attachments(item))
    return found


def _api_candidate(turn: list) -> Optional[list]:
    """Premier candidat de reponse d'un tour (``turn[3][0][0]``)."""
    if not isinstance(turn, list) or len(turn) < 4:
        return None
    candidates = turn[3][0] if isinstance(turn[3], list) else None
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], list):
        return candidates[0]
    return None


def _api_assistant_text(turn: list) -> str:
    """Texte markdown brut de la reponse (candidat -> liste de morceaux)."""
    candidate = _api_candidate(turn)
    chunks = (
        candidate[1]
        if candidate is not None and len(candidate) > 1 and isinstance(candidate[1], list)
        else []
    )
    return "\n".join(chunk for chunk in chunks if isinstance(chunk, str))


def _api_candidate_fields(candidate: Optional[list]) -> Dict[str, Any]:
    """Champs structures d'un candidat (dicts protobuf du champ 12).

    Les champs (citations `44`, fichiers `59`, blocs de code `39`...) vivent
    dans des dictionnaires imbriques ; on fusionne ceux du candidat (la premiere
    valeur rencontree gagne). La position du dict n'est pas fiable (elle varie
    selon la taille de page demandee) : on parcourt tout le candidat.
    """
    if candidate is None:
        return {}
    fields: Dict[str, Any] = {}
    for node in _api_walk(candidate):
        if isinstance(node, dict):
            for key, value in node.items():
                fields.setdefault(key, value)
    return fields


def _api_source_label(node: list) -> str:
    """Piece jointe citee -> libelle de source, format DOM ``JSON: donnees.json``."""
    name = _api_attachment_name(node)
    if not name:
        return ""
    if "." in name:
        return f"{name.rsplit('.', 1)[1].upper()}: {name}"
    mime = _api_attachment_mime(node)
    return f"{mime.split('/')[-1].upper()}: {name}" if mime else name


def _api_citations(candidate: Optional[list]) -> Dict[str, str]:
    """Marqueurs ``[cite: N]`` -> libelle de source (champ ``44``)."""
    fields = _api_candidate_fields(candidate)
    out: Dict[str, str] = {}
    for entry in fields.get("44") or []:
        marker: Optional[str] = None
        nodes: List[list] = []
        for node in _api_walk(entry):
            for item in _api_values(node):
                if marker is None and isinstance(item, str) and item.startswith("[cite:"):
                    marker = item
            nodes.extend(_api_find_attachments(node))
        if not marker or not nodes:
            continue
        label = _api_source_label(nodes[0])
        if label:
            out[marker] = label
    return out


def _api_file_tags(candidate: Optional[list]) -> Dict[str, str]:
    """Marqueurs ``[file-tag: ..]`` -> lien markdown du fichier (champ ``59``)."""
    fields = _api_candidate_fields(candidate)
    out: Dict[str, str] = {}
    for entry in fields.get("59") or []:
        marker: Optional[str] = None
        nodes: List[list] = []
        for node in _api_walk(entry):
            for item in _api_values(node):
                if marker is None and isinstance(item, str) and item.startswith("[file-tag:"):
                    marker = item
            nodes.extend(_api_find_attachments(node))
        if not marker or not nodes:
            continue
        markdown = _api_attachment_markdown(nodes[0])
        if markdown:
            out[marker] = markdown
    return out


def _api_generated_images(candidate: Optional[list]) -> Dict[str, str]:
    """URL interne d'image generee -> markdown de l'image reelle.

    Le texte de la reponse contient une URL ``image_generation_content`` (non
    telechargeable) ; le champ structure associe porte le nom du fichier et
    l'URL ``lh3.googleusercontent.com/gg/..`` de l'image (telechargeable). Le
    noeud le plus proche qui porte les deux (URL + piece jointe) gagne.
    """
    if candidate is None:
        return {}
    best: Dict[str, Tuple[int, str]] = {}
    for node in _api_walk(candidate):
        urls = [
            item
            for sub in _api_walk(node)
            for item in _api_values(sub)
            if isinstance(item, str) and "image_generation_content" in item
        ]
        if not urls:
            continue
        attachments = _api_find_attachments(node)
        if not attachments:
            continue
        markdown = _api_attachment_markdown(attachments[0])
        if not markdown:
            continue
        size = sum(1 for _ in _api_walk(node))
        for url in urls:
            previous = best.get(url)
            if previous is None or size < previous[0]:
                best[url] = (size, markdown)
    return {url: markdown for url, (_size, markdown) in best.items()}


def _api_tools(turn: list, user_video: bool = False) -> List[str]:
    """Outils/etapes d'un tour, mappes vers les libelles du parseur DOM.

    L'API nomme les outils par identifiant (`data_analysis_tool`, `google`..) et
    porte un libelle localise ; on retablit les libelles francais stables du
    DOM (« Exécution de code », « Génération d'images », ..).
    """
    if not isinstance(turn, list) or len(turn) < 5:
        return []
    tools: List[str] = []
    for field in (4, 5):
        entries = turn[3][field] if len(turn[3]) > field else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 2:
                continue
            inner = entry[1]
            if not isinstance(inner, list) or not inner:
                continue
            name = inner[0] if isinstance(inner[0], str) else ""
            label = ""
            if len(inner) > 1 and isinstance(inner[1], list):
                parts = [p for p in inner[1] if isinstance(p, str)]
                label = " ".join(parts)
            mapped = API_TOOL_LABELS.get(name)
            if name == "data_analysis_tool" and any(
                marker in label.lower() for marker in API_TOOL_IMAGE_MARKERS
            ):
                mapped = "Génération d'images"
            mapped = mapped or name
            if mapped and mapped not in tools:
                tools.append(mapped)
    if user_video and "Analyse vidéo" not in tools:
        # marqueur DOM `.luminous-video-duration-pill` : une video jointe au tour
        tools.append("Analyse vidéo")
    return tools


def _api_model(turn: list) -> Optional[str]:
    """Identifiant du modele de la reponse (dernier champ du tour)."""
    if not isinstance(turn, list) or len(turn) < 4 or not isinstance(turn[3], list):
        return None
    for value in reversed(turn[3]):
        if isinstance(value, str) and value.startswith("gemini-"):
            return value
    return None


def _api_timestamp(turn: list) -> Optional[str]:
    """Horodatage du tour (``[epoch, nanos]``) en ISO Z."""
    if not isinstance(turn, list) or len(turn) < 5:
        return None
    stamp = turn[4]
    if not isinstance(stamp, list) or not stamp or not isinstance(stamp[0], (int, float)):
        return None
    seconds = float(stamp[0])
    nanos = float(stamp[1]) if len(stamp) > 1 and isinstance(stamp[1], (int, float)) else 0.0
    return normalize_timestamp(seconds + nanos / 1e9)


def _api_trim_fences(text: str) -> str:
    """Retire les lignes vides finales des blocs ``` (comme le rendu DOM).

    Le payload termine le corps d'un bloc par une ligne vide
    (``print(x)\\n\\n``` ``) : le rendu DOM, lui, n'a pas de ligne vide avant la
    cloture. On aligne pour que ``code_blocks`` soit identique.
    """
    def repl(match: "re.Match[str]") -> str:
        info, body = match.group(1), match.group(2)
        if not body.strip():
            return match.group(0)
        return f"```{info}\n{body.rstrip()}\n```"

    return API_FENCE_BLOCK_RE.sub(repl, text)


def _api_tidy_blank_lines(text: str) -> str:
    """Compacte les lignes vides hors blocs de code (convention du DOM)."""
    out: List[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return "\n".join(out)


def _api_normalize_text(
    text: str,
    *,
    citations: Dict[str, str],
    file_tags: Dict[str, str],
    generated_images: Dict[str, str],
) -> str:
    """Markdown du payload -> markdown du schema (annotations retirees)."""
    for marker, markdown in file_tags.items():
        if marker in text:
            text = text.replace(marker, markdown)
    for url, markdown in generated_images.items():
        if url in text:
            text = text.replace(url, markdown)
    text = API_GENERATED_IMAGE_RE.sub("", text)
    text = API_FILE_TAG_RE.sub("", text)
    text = API_CITE_RE.sub("", text)
    text = API_FENCE_RE.sub(lambda m: f"```{m.group(1)}", text)
    text = _api_trim_fences(text)
    text = API_BULLET_RE.sub(lambda m: f"{m.group(1)}- ", text)
    text = _api_tidy_blank_lines(text)
    return text.strip()


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
        # libelle d'accessibilite des reponses recentes (« Gemini a dit »)
        ".screen-reader-model-response-label",
        "[class*='model-response-label']",
    )

    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        extra = extra or {}
        if extra.get("api"):
            # chemin API (payload batchexecute `hNvQHb`) : le service passe le
            # payload JSON brut dans `extra["api"]`, le HTML est ignore.
            return self.parse_api(
                extra["api"], conversation_id=conversation_id, extra=extra
            )
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
            timestamp = self._timestamp_of(node)
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
            artifacts: List[str] = []
            if role == "assistant":
                metadata["tokens"] = None
                artifacts = self._artifacts_of(node)
                if not model:
                    # le pied de reponse est un frere du noeud de texte :
                    # on sonde l'ancetre direct, puis la page entiere
                    model = self._model_from_footer(node.parent) or self._model_from_footer(soup)
            message = self.msg(role, content, timestamp, metadata)
            if artifacts:
                message.artifacts = artifacts
            if role == "assistant":
                sources = self._sources_of(node)
                if sources:
                    message.sources = sources
                tools = self._tools_of(node)
                if tools:
                    message.tools = tools
            messages.append(message)

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

    def parse_api(
        self,
        payload: Dict[str, Any],
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        """Construit la conversation depuis le payload brut de l'API Gemini.

        ``payload`` = ``{"conversation_id", "title", "turns": [tour, ..]}`` ou
        chaque tour vient de la reponse ``hNvQHb`` de ``batchexecute`` (pages
        fusionnees dans l'ordre du fil). Le texte des reponses est normalise
        vers le markdown du schema : annotations de fence (``?code_reference``),
        marqueurs ``[cite: N]`` (-> ``sources``), ``[file-tag: ..]`` (-> lien du
        fichier genere), URL d'image generee (-> image markdown), puces ``*``
        alignees sur le rendu DOM (``-``). Les pieces jointes du tour
        utilisateur sont ajoutees en markdown (images telechargeables, fichiers
        via l'URL de telechargement authentifiee).
        """
        extra = extra or {}
        turns = payload.get("turns") if isinstance(payload, dict) else None
        if not isinstance(turns, list) or not turns:
            raise ParseError("gemini api: aucune donnee de conversation")

        messages: List[Any] = []
        models: List[str] = []
        for turn in turns:
            if not isinstance(turn, list):
                continue
            candidate = _api_candidate(turn)
            citations = _api_citations(candidate)
            file_tags = _api_file_tags(candidate)
            generated = _api_generated_images(candidate)

            parts = _api_parts(turn[2] if len(turn) > 2 else None)
            text = "\n\n".join(
                chunk for chunk in (_api_part_text(part) for part in parts) if chunk
            )
            text = _api_normalize_text(
                text, citations={}, file_tags={}, generated_images={}
            )
            attachments: List[str] = []
            video_attached = False
            for part in parts:
                for node in _api_message_attachments(part):
                    markdown = _api_attachment_markdown(node)
                    if markdown and markdown not in attachments:
                        attachments.append(markdown)
                    if _api_attachment_mime(node).startswith("video/"):
                        video_attached = True
            if attachments:
                block = "\n\n".join(attachments)
                if block not in text:
                    text = f"{text}\n\n{block}".strip() if text else block
            timestamp = _api_timestamp(turn)
            user_id = ""
            if len(turn) > 0 and isinstance(turn[0], list) and len(turn[0]) > 1:
                user_id = str(turn[0][1] or "")
            if text or attachments:
                messages.append(
                    self.msg("user", text, timestamp, None, message_id=user_id)
                )

            assistant_raw = _api_assistant_text(turn)
            assistant = _api_normalize_text(
                assistant_raw,
                citations=citations,
                file_tags=file_tags,
                generated_images=generated,
            )
            tools = _api_tools(turn, user_video=video_attached)
            artifacts: List[str] = []
            if re.search(r"```xml[^\n]*\n[^`]*<svg", assistant):
                # artefact canvas/SVG : le DOM le signale par `svg-code-block`
                artifacts.append("SVG")
                if "Canvas" not in tools:
                    tools.append("Canvas")
            sources = [
                label for marker, label in citations.items() if marker in assistant_raw
            ]
            model = _api_model(turn)
            if model:
                models.append(model)
            metadata: Dict[str, Any] = {"tokens": None}
            # l'id de la reponse est porte par le candidat (`rc_..`), avec repli
            # sur le champ 3 de la structure assistant (positions variables)
            assistant_id = ""
            if candidate is not None and isinstance(candidate[0], str):
                assistant_id = candidate[0]
            elif len(turn) > 3 and isinstance(turn[3], list) and len(turn[3]) > 3:
                assistant_id = str(turn[3][3] or "")
            message = self.msg(
                "assistant",
                assistant,
                timestamp,
                metadata,
                message_id=assistant_id,
                model=model,
            )
            if tools:
                message.tools = tools
            if artifacts:
                message.artifacts = artifacts
            if sources:
                message.sources = sources
            # une reponse vide (rendu en echec, image pure...) est conservee
            # pour ne pas fusionner deux requetes utilisateur consecutives
            # (meme convention que le parseur DOM).
            if assistant or tools or artifacts or sources or (
                messages and messages[-1].role == "user"
            ):
                messages.append(message)

        if not messages:
            raise ParseError("gemini api: aucun message extrait")

        title = payload.get("title") or extra.get("title_hint")
        title = clean_ui_title(title) or clean_ui_title(extra.get("title_hint"))
        if title:
            title = re.sub(r"\s+[—-]\s*Gemini\s*$", "", title)

        conv_id = (
            conversation_id
            or (payload.get("conversation_id") if isinstance(payload, dict) else None)
            or extra.get("conversation_id")
        )
        if not conv_id:
            conv_id = "unknown"

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Gemini conversation",
            messages=messages,
            model=self.majority(models),
        )
        self.log_parse(conv, model=conv.model, title=title)
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
        cls._svg_code_blocks(node)
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
    def _svg_code_blocks(cls, node) -> None:
        """Rend le code source SVG d'un artefact ``<svg-code-block>``."""
        for block in list(node.find_all("svg-code-block")):
            svg = block.find("svg")
            if svg is None:
                continue
            code = str(svg).strip()
            if not code:
                continue
            block.replace_with(NavigableString("\n```svg\n" + code + "\n```\n"))

    @staticmethod
    def _artifacts_of(node) -> List[str]:
        """Titres des artefacts d'un tour (SVG, canvas, éditeur immersif...)."""
        titles: List[str] = []
        for block in node.select("svg-code-block"):
            label = block.select_one(".svg-label, [class*='svg-label']")
            title = label.get_text(" ", strip=True) if label is not None else "SVG"
            title = clean_ui_title(title) or "SVG"
            if title not in titles:
                titles.append(title)
        for block in node.select("immersive-editor, [class*='canvas-title']"):
            title = clean_ui_title(block.get_text(" ", strip=True)[:80])
            if title and title not in titles:
                titles.append(title)
        return titles

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

    # -- sources / timestamps / outils -----------------------------------------

    @staticmethod
    def _response_root(node):
        """Remonte au ``<model-response>`` englobant (sources/temps hors contenu)."""
        if node is None:
            return None
        if getattr(node, "name", None) == "model-response":
            return node
        return node.find_parent("model-response") or node

    @classmethod
    def _timestamp_of(cls, node) -> Optional[str]:
        """Horodatage d'un tour, si le DOM en expose un (sinon ``None``).

        Gemini n'affiche generalement pas l'heure des messages ; on sonde les
        marqueurs usuels (``<time datetime>``, ``data-timestamp``...) pour ne
        rien perdre quand une variante du DOM les fournit.
        """
        node = cls._response_root(node)
        if node is None:
            return None
        for sel in TIMESTAMP_SELECTORS:
            try:
                elements = node.select(sel)
            except Exception:
                continue
            for el in elements:
                raw = (
                    el.get("datetime")
                    or el.get("data-timestamp")
                    or el.get("data-utc")
                    or el.get_text(" ", strip=True)
                )
                stamp = normalize_timestamp(raw)
                if stamp:
                    return stamp
        return None

    @classmethod
    def _sources_of(cls, node) -> List[str]:
        """Citations d'une reponse : puces inline + cartes de ``sources-list``.

        Les puces (``source-inline-chip``) portent le domaine/titre dans
        ``.source-title`` et le detail dans l'``aria-label`` (« citation de
        JSON: donnees.json », « citations de A, B et C »). Les cartes des
        ``sources-list`` (quand le panneau est monte) portent un lien direct.
        """
        node = cls._response_root(node)
        if node is None:
            return []
        sources: List[str] = []
        for chip in node.select(SOURCE_CHIP_SELECTOR):
            for label in cls._chip_sources(chip):
                if label and label not in sources:
                    sources.append(label)
        for container in node.select(", ".join(SOURCE_LIST_SELECTORS)):
            for anchor in container.select("a[href]"):
                label = cls._anchor_source(anchor)
                if label and label not in sources:
                    sources.append(label)
        return sources

    @classmethod
    def _chip_sources(cls, chip) -> List[str]:
        """Libelles d'une puce de citation (une puce peut en couvrir plusieurs)."""
        hrefs = [
            a.get("href").strip()
            for a in chip.select("a[href]")
            if (a.get("href") or "").strip()
        ]
        label_el = chip.select_one(".source-title")
        title = label_el.get_text(" ", strip=True) if label_el is not None else ""
        if hrefs:
            out: List[str] = []
            for href in hrefs:
                out.append(f"{title} — {href}" if title and title not in href else href)
            return out
        button = chip.find("button") or chip
        detail = cls._citation_detail(button.get("aria-label") or "")
        if detail:
            return cls._split_citations(detail)
        return [title] if title else []

    @staticmethod
    def _citation_detail(aria: str) -> str:
        """Partie utile d'un aria-label de source (« citation(s) de … »)."""
        match = _CITATION_ARIA_RE.search(aria or "")
        if not match:
            return ""
        return re.sub(r"\s+", " ", match.group("detail")).strip()

    @staticmethod
    def _split_citations(detail: str) -> List[str]:
        """« A, B et C » -> [A, B, C] (sans doublon)."""
        parts = re.split(r",\s*|\s+et\s+", detail or "")
        out: List[str] = []
        for part in parts:
            cleaned = re.sub(r"\s+", " ", part).strip(" .;")
            if cleaned and cleaned not in out:
                out.append(cleaned)
        return out

    @staticmethod
    def _anchor_source(anchor) -> str:
        """Lien de carte de source -> « titre — url » (ou l'un des deux)."""
        href = (anchor.get("href") or "").strip()
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not href:
            return title
        if title and title != href and title not in href:
            return f"{title} — {href}"
        return title or href

    @classmethod
    def _tools_of(cls, node) -> List[str]:
        """Outils/etapes utilises, deduits des marqueurs structurels du tour.

        Gemini ne pose pas de badge textuel stable ; ces marqueurs (sortie de
        code, images generees, canvas, video) signalent l'outil reellement mis
        en oeuvre sans inventer de libelle generique.
        """
        if node is None:
            return []
        # certains marqueurs (duree video) vivent dans la piece jointe du tour :
        # on elargit au conteneur du tour quand il existe.
        turn = node.find_parent(class_="conversation-container") or node
        tools: List[str] = []
        for selectors, label in TOOL_MARKERS:
            for sel in selectors:
                try:
                    found = turn.select_one(sel)
                except Exception:
                    found = None
                if found is not None:
                    if label not in tools:
                        tools.append(label)
                    break
        return tools
