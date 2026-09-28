"""Parser Grok (xAI) : construit la conversation depuis l'API REST.

Grok n'est pas scrapable au navigateur (Cloudflare bloque le Chromium pilote) ;
les donnees viennent de l'API authentifiee par cookies, passees en `extra` :

  extra = {
    "conversation": {conversationId, title, createTime, modifyTime, ...},
    "responses": [{responseId, message, sender, createTime, model, ...}, ...]
  }
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from ..utils.cleanup import clean_grok_text
from .base import BaseParser, ParseError

ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant",
            "system": "system", "tool": "tool"}

#: base publique des assets Grok (`users/.../content`, images, fichiers generes)
ASSETS_BASE = "https://assets.grok.com"


def _asset_url(key: str) -> str:
    """URL absolue d'un asset Grok a partir de sa cle (`users/.../content`)."""
    key = str(key or "").strip()
    if key.startswith(("http://", "https://")):
        return key
    return f"{ASSETS_BASE}/{key.lstrip('/')}"


def _md_label(name: str) -> str:
    """Echappe le libelle d'un lien/image markdown."""
    return str(name or "").replace("[", "\\[").replace("]", "\\]")


# balises de citation inline `<grok:render ... citation_card ...>`
_RENDER_RE = re.compile(
    r"<grok:render\b(?P<attrs>[^>]*)>(?P<body>.*?)</grok:render>",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r"(\w+)=[\"']([^\"']*)[\"']")
_CITATION_ID_RE = re.compile(
    r"<argument\b[^>]*name=[\"']citation_id[\"'][^>]*>(?P<id>.*?)</argument>",
    re.DOTALL | re.IGNORECASE,
)

#: libelles lisibles des outils Grok, indexes par nom normalise (minuscules sans
#: separateur) : couvre les `tool_name` XML (`web_search`) et les cles
#: `toolUsageCards` (`webSearch`).
_TOOL_LABELS = {
    "websearch": "Recherche web",
    "websearchwithsnippets": "Recherche web (extraits)",
    "browsepage": "Navigation web",
    "browsertab": "Navigation web",
    "openpage": "Ouverture de page",
    "openpagewithfind": "Recherche dans une page",
    "xkeywordsearch": "Recherche X",
    "xsearch": "Recherche X",
    "xsemanticsearch": "Recherche X (sémantique)",
    "xthreadfetch": "Lecture de fil X",
    "xusersearch": "Recherche d'utilisateur X",
    "viewxvideo": "Consultation de vidéo X",
    "readfile": "Lecture de fichier",
    "writefile": "Écriture de fichier",
    "viewimage": "Analyse d'image",
    "bash": "Exécution de commandes",
    "initterminalsession": "Initialisation du terminal",
    "codeexecution": "Exécution de code",
    "mcp": "Connecteurs MCP",
    "chatroomsend": "Envoi au chat",
}
#: nom interne d'un outil dans une etape `tool_usage_card`
_TOOL_NAME_RE = re.compile(
    r"<xai:tool_name>(?P<name>.*?)</xai:tool_name>", re.DOTALL | re.IGNORECASE
)


def _tool_key(name: str) -> str:
    """Normalise un nom d'outil pour la table des libelles."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _tool_label(name: str) -> str:
    """Libelle lisible d'un outil Grok (nom interne -> francais).

    Les noms inconnus sont conserves tels quels : l'objectif est de refleter le
    payload, pas d'inventer une taxonomie.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    return _TOOL_LABELS.get(_tool_key(raw), raw)


def _add_tool(tools: List[str], label: str) -> None:
    if label and label not in tools:
        tools.append(label)


def _cards(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cartes `cardAttachmentsJson` d'une reponse (chaines JSON -> dictionnaires)."""
    cards: List[Dict[str, Any]] = []
    for item in resp.get("cardAttachmentsJson") or []:
        card: Any = item
        if isinstance(item, str):
            try:
                card = json.loads(item)
            except json.JSONDecodeError:
                continue
        if isinstance(card, dict):
            cards.append(card)
    return cards


def _citation_urls(resp: Dict[str, Any]) -> Dict[str, str]:
    """Carte `citation_card` -> URL, indexee par identifiant de carte."""
    urls: Dict[str, str] = {}
    for card in _cards(resp):
        if card.get("cardType") == "citation_card" or card.get("type") == "render_inline_citation":
            card_id = card.get("id")
            url = card.get("url")
            if card_id and url:
                urls[str(card_id)] = str(url)
    return urls


def _render_citations(message: str, resp: Dict[str, Any]) -> str:
    """Remplace les citations inline par un lien markdown `[n](url)`.

    Grok insere des balises `<grok:render ... citation_card>` que
    `clean_grok_text` supprimerait avec la source. On les convertit avant.
    """
    urls = _citation_urls(resp)
    if not urls or "<grok:render" not in message:
        return message

    def repl(match: "re.Match[str]") -> str:
        attrs = dict(_ATTR_RE.findall(match.group("attrs")))
        url = urls.get(str(attrs.get("card_id") or ""))
        if not url:
            return match.group(0)
        ref = _CITATION_ID_RE.search(match.group("body"))
        label = ref.group("id").strip() if ref else "source"
        return f"[{_md_label(label)}]({url})"

    return _RENDER_RE.sub(repl, message)


def _steps(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Etapes de rendu d'une reponse (`steps`), filtrees sur les dictionnaires.

    Chaque etape porte des `tags` (`header`, `tool_usage_card`,
    `raw_function_result`, `summary`, `decision`...) et le texte associe.
    """
    return [s for s in (resp.get("steps") or []) if isinstance(s, dict)]


def _step_tool_names(step: Dict[str, Any]) -> List[str]:
    """Noms d'outils d'une etape (`tool_name` XML et cles `toolUsageCards`)."""
    names: List[str] = []
    joined = "\n".join(str(part) for part in (step.get("text") or []))
    names.extend(_TOOL_NAME_RE.findall(joined))
    for card in step.get("toolUsageCards") or []:
        if not isinstance(card, dict):
            continue
        # la carte porte l'identifiant + une cle par outil (`bash`, `mcp`...)
        for key in card:
            if key != "toolUsageCardId":
                names.append(str(key))
    return names


def _tools(resp: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[str]:
    """Outils/etapes utilises par une reponse, en libelles lisibles.

    Source principale : les etapes `tool_usage_card` (`<xai:tool_name>` et
    `toolUsageCards`). Certains tours exposent des resultats de recherche sans
    carte d'outil : on en deduit alors le libelle correspondant.
    """
    tools: List[str] = []
    steps = _steps(resp)
    for step in steps:
        tags = step.get("tags") or []
        if "tool_usage_card" not in tags and not step.get("toolUsageCards"):
            continue
        for name in _step_tool_names(step):
            _add_tool(tools, _tool_label(name))
    has_web = bool(resp.get("webSearchResults") or resp.get("citedWebSearchResults"))
    has_web = has_web or any(
        s.get("webSearchResults") or s.get("citedWebSearchResults") for s in steps
    )
    if has_web:
        _add_tool(tools, "Recherche web")
    has_x = bool(resp.get("xposts") or resp.get("citedXposts"))
    has_x = has_x or any(s.get("xposts") or s.get("citedXposts") for s in steps)
    if has_x:
        _add_tool(tools, "Recherche X")
    if resp.get("ragResults") or resp.get("citedRagResults"):
        _add_tool(tools, "Recherche documentaire")
    if resp.get("connectorSearchResults") or resp.get("citedConnectorSearchResults"):
        _add_tool(tools, "Recherche connecteurs")
    if resp.get("collectionSearchResults") or resp.get("citedCollectionSearchResults"):
        _add_tool(tools, "Recherche collections")
    if resp.get("searchProductResults"):
        _add_tool(tools, "Recherche produits")
    if resp.get("generatedImageUrls") or any(
        c.get("cardType") == "generated_image_card" for c in cards
    ):
        _add_tool(tools, "Génération d'image")
    return tools


def _urls_of(items: Any) -> List[str]:
    """URLs d'une liste de resultats API (`{url, title...}` ou chaines)."""
    urls: List[str] = []
    for item in items or []:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
        elif isinstance(item, str):
            url = item.strip()
        else:
            url = ""
        if url and url not in urls:
            urls.append(url)
    return urls


def _xpost_url(post: Any) -> str:
    """URL d'un post X depuis `{username, postId}` (ou `url` directe)."""
    if not isinstance(post, dict):
        return ""
    url = str(post.get("url") or "").strip()
    if url:
        return url
    username = str(post.get("username") or "").strip()
    post_id = str(post.get("postId") or "").strip()
    if username and post_id:
        return f"https://x.com/{username}/status/{post_id}"
    return ""


def _sources(resp: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[str]:
    """URLs des sources/citations web d'une reponse, dedupliquees.

    Priorite aux citations reellement utilisees (`citedWebSearchResults` et
    cartes `citation_card`) ; a defaut, les resultats exposes par la recherche
    web (`webSearchResults`) et les posts X (`xposts`). L'API Grok ne fournit
    pas de sources pour un tour sans recherche : la liste est alors vide.
    """
    sources: List[str] = []
    steps = _steps(resp)
    for url in _urls_of(resp.get("citedWebSearchResults")):
        if url not in sources:
            sources.append(url)
    for step in steps:
        for url in _urls_of(step.get("citedWebSearchResults")):
            if url not in sources:
                sources.append(url)
    for card in cards:
        kind = card.get("cardType") or card.get("type")
        if kind in ("citation_card", "render_inline_citation"):
            url = str(card.get("url") or "").strip()
            if url and url not in sources:
                sources.append(url)
    if sources:
        return sources
    for url in _urls_of(resp.get("webSearchResults")):
        sources.append(url)
    for step in steps:
        for url in _urls_of(step.get("webSearchResults")):
            if url not in sources:
                sources.append(url)
    for post in list(resp.get("xposts") or []) + list(resp.get("citedXposts") or []):
        url = _xpost_url(post)
        if url and url not in sources:
            sources.append(url)
    for step in steps:
        for post in (step.get("xposts") or []) + (step.get("citedXposts") or []):
            url = _xpost_url(post)
            if url and url not in sources:
                sources.append(url)
    return sources


def _attachment_items(resp: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pieces jointes d'un tour (nom, type MIME, cle d'asset).

    Grok expose les fichiers joins dans `fileAttachmentAssetMetadata` (riche :
    nom + mime + cle) et `fileAttachmentsMetadata` (repli). Les cles sont
    relatives (`users/.../content`) et servies par `assets.grok.com`.
    """
    items: List[Dict[str, str]] = []
    for meta in resp.get("fileAttachmentAssetMetadata") or []:
        if not isinstance(meta, dict):
            continue
        key = str(meta.get("key") or "").strip()
        if not key:
            continue
        items.append({
            "name": str(meta.get("name") or "fichier").strip() or "fichier",
            "mime": str(meta.get("mimeType") or "").strip().lower(),
            "key": key,
        })
    if items:
        return items
    for meta in resp.get("fileAttachmentsMetadata") or []:
        if not isinstance(meta, dict):
            continue
        key = str(meta.get("fileUri") or "").strip()
        if not key:
            continue
        items.append({
            "name": str(meta.get("fileName") or "fichier").strip() or "fichier",
            "mime": str(meta.get("fileMimeType") or "").strip().lower(),
            "key": key,
        })
    return items


def _attachments_markdown(resp: Dict[str, Any]) -> str:
    """Markdown des pieces jointes : images en `![...]`, autres en `[...]`."""
    entries: List[str] = []
    for item in _attachment_items(resp):
        url = _asset_url(item["key"])
        label = _md_label(item["name"])
        if item["mime"].startswith("image/"):
            entries.append(f"![{label}]({url})")
        else:
            entries.append(f"[{label}]({url})")
    # images generees (le cas echeant) : URL deja absolues ou cles d'asset
    for url in resp.get("generatedImageUrls") or []:
        if isinstance(url, str) and url.strip():
            entries.append(f"![image]({_asset_url(url)})")
    seen: set = set()
    return "\n\n".join(e for e in entries if not (e in seen or seen.add(e)))


def _rendered_files(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cartes `rendered_file_card` d'une reponse (fichiers generes par Grok).

    Grok renvoie ces cartes dans `cardAttachmentsJson` (liste de chaines JSON)
    quand le tour produit un fichier (TXT, PDF, DOCX...) sans message texte.
    """
    return [
        card for card in _cards(resp)
        if card.get("type") == "render_file" or card.get("cardType") == "rendered_file_card"
    ]


def _artifact_titles(resp: Dict[str, Any]) -> List[str]:
    """Titres des artefacts/canvas d'une reponse (nom du fichier rendu).

    Grok produit un artefact (canvas HTML, fichier rendu) sous forme de carte
    `render_file`/`rendered_file_card` dans `cardAttachmentsJson`. L'API
    n'expose que la reference du fichier (nom + cle), pas son contenu : on
    conserve le nom comme titre de l'artefact dans `Message.artifacts`.
    """
    titles: List[str] = []
    for card in _cards(resp):
        kind = f"{card.get('type') or ''} {card.get('cardType') or ''}".lower()
        if not any(tag in kind for tag in ("render_file", "rendered_file_card",
                                           "artifact", "canvas")):
            continue
        title = str(card.get("file_name") or card.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def _rendered_files_text(cards: List[Dict[str, Any]]) -> str:
    """Texte de substitution pour un tour assistant reduit a un fichier genere."""
    lines: List[str] = []
    for card in cards:
        name = str(card.get("file_name") or "fichier").strip()
        mime = str(card.get("mime_type") or card.get("content_type") or "").strip()
        size = card.get("file_size")
        details = ", ".join(
            part for part in (mime, f"{size} o" if isinstance(size, int) else "") if part
        )
        lines.append(f"Fichier généré : {name}" + (f" ({details})" if details else ""))
    return "\n".join(lines)


def _stream_error_message(resp: Dict[str, Any]) -> str:
    """Texte du premier `streamError` d'une reponse (quota, erreur de flux).

    Grok renvoie parfois une reponse assistant au `message` vide accompagnee de
    `streamErrors` (ex. « You've reached your usage limit »). La conserver
    evite de fusionner les deux messages `user` encadrants, ce qui decalerait
    tout l'appariement des tours.
    """
    errors = resp.get("streamErrors") or []
    if not errors:
        meta = resp.get("metadata") or {}
        errors = (meta.get("request_metadata") or {}).get("stream_errors") or []
    for err in errors:
        if isinstance(err, dict):
            message = err.get("message")
            if message and str(message).strip():
                return str(message).strip()
    return ""


class GrokParser(BaseParser):
    service_name = "grok"

    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        extra = extra or {}
        responses = extra.get("responses") or []
        conv_meta = extra.get("conversation") or {}
        if not responses:
            raise ParseError("grok: aucune reponse dans les donnees API")

        messages: List[Any] = []
        models: List[str] = []
        for resp in responses:
            text = clean_grok_text(_render_citations(resp.get("message") or "", resp)).strip()
            cards = _rendered_files(resp)
            files_text = _rendered_files_text(cards)
            attachments = _attachments_markdown(resp)
            all_cards = _cards(resp)
            tools = _tools(resp, all_cards)
            sources = _sources(resp, all_cards)
            if not text:
                # reponse vide mais porteuse d'une erreur de flux : on la garde
                # pour preserver l'alternance des roles (cas du quota atteint).
                text = _stream_error_message(resp)
            if not text and files_text:
                # tour assistant reduit a un fichier genere (resultat.txt, PDF...)
                text = files_text
                files_text = ""
            if not text and attachments:
                # tour sans texte reduit a une piece jointe (image, document...)
                text = attachments
                attachments = ""
            if not text:
                role = ROLE_MAP.get(
                    str(resp.get("sender") or "assistant").lower(), "assistant"
                )
                if role != "assistant":
                    continue
                # tour assistant totalement vide : on le conserve vide pour ne pas
                # fusionner les deux messages `user` encadrants (alternance).
            else:
                if files_text and files_text not in text:
                    text = f"{text}\n\n{files_text}"
                if attachments and attachments not in text:
                    text = f"{text}\n\n{attachments}"
            text = text.strip()
            sender = str(resp.get("sender") or "assistant").lower()
            role = ROLE_MAP.get(sender, "assistant")
            model = resp.get("model") or None
            if role == "assistant" and model:
                models.append(str(model))
            metadata: Dict[str, Any] = {"tokens": None}
            if cards:
                metadata["generated_files"] = cards
            message = self.msg(
                role,
                text,
                resp.get("createTime"),
                metadata,
                message_id=str(resp.get("responseId") or ""),
                model=str(model) if model else None,
            )
            artifacts = _artifact_titles(resp)
            if artifacts:
                message.artifacts = artifacts
            if tools:
                message.tools = tools
            if sources:
                message.sources = sources
            messages.append(message)

        if not messages:
            raise ParseError("grok: aucun message exploitable")

        model = self.majority(models)
        conv_id = (
            conversation_id
            or conv_meta.get("conversationId")
            or extra.get("conversation_id")
        )
        if not conv_id:
            raise ParseError("grok: conversation_id manquant")

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=str(conv_meta.get("title") or extra.get("title_hint") or "Grok conversation"),
            messages=messages,
            model=model,
            started_at=conv_meta.get("createTime"),
            last_message_at=conv_meta.get("modifyTime"),
        )
        self.log_parse(conv, model=model, title=conv.title)
        return self.check(conv)
