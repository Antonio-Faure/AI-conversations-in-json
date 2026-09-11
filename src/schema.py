"""Schema JSON standardise pour les conversations IA.

Format de sortie (un fichier par conversation) :

{
  "conversation_id": "abc123",
  "platform": "chatgpt",
  "title": "...",
  "model": "gpt-4",
  "started_at": "2025-01-15T10:30:00Z",
  "last_message_at": "2025-01-15T11:45:00Z",
  "exported_at": "...",
  "messages": [
    {
      "conversation_id": "abc123",
      "message_id": "uuid",
      "role": "user" | "assistant",
      "platform": "chatgpt",
      "model": "gpt-4",
      "timestamp": "...",
      "texte": "...",
      "code_blocks": [{"language": "python", "code": "..."}]
    }
  ]
}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

VALID_ROLES = ("user", "assistant", "system", "tool")
KNOWN_PLATFORMS = ("chatgpt", "claude", "gemini", "perplexity")

#: blocs ```lang ... ``` : cloture en debut de ligne (evite de fermer sur un
#: ``` present dans le code lui-meme)
CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)


class SchemaError(ValueError):
    """Levee quand un objet ne respecte pas le schema standardise."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    """Formate une datetime en ISO-8601 UTC avec suffixe Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> Optional[datetime]:
    """Accepte une datetime, un epoch, ou une chaine ISO-8601 (Z ou offset).

    Retourne une datetime aware UTC, ou None si non parseable/vide.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
        return dt
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def normalize_timestamp(value: Any) -> Optional[str]:
    """Normalise n'importe quelle representation temporelle en ISO Z (ou None)."""
    dt = parse_iso(value)
    return to_iso_z(dt) if dt else None


def extract_code_blocks(text: Optional[str]) -> List["CodeBlock"]:
    """Extrait les blocs ```lang ... ``` du markdown, dans l'ordre du texte."""
    blocks: List[CodeBlock] = []
    for match in CODE_FENCE_RE.finditer(text or ""):
        language = (match.group(1) or "").strip()
        code = match.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        blocks.append(CodeBlock(language=language, code=code))
    return blocks


def normalize_messages(messages: List["Message"]) -> List["Message"]:
    """Garantit l'alternance user/assistant et supprime les doublons consecutifs.

    - deux messages consecutifs du meme role sont fusionnes (texte, code_blocks,
      metadonnees ; le premier message_id/modele/timestamp non vide est garde) ;
    - un message dont le texte est deja contenu dans le precedent est ignore.
    Applique par les parsers (`BaseParser.check`) et la regeneration d'exports.
    """
    merged: List[Message] = []
    for message in messages:
        if not merged or merged[-1].role != message.role:
            merged.append(message)
            continue
        previous = merged[-1]
        text = (message.texte or "").strip()
        prev_text = (previous.texte or "").strip()
        if not text:
            pass
        elif text == prev_text or text in prev_text:
            pass
        elif prev_text and prev_text in text:
            previous.texte = message.texte
        else:
            previous.texte = (previous.texte + "\n\n" + message.texte).strip()
        for block in message.code_blocks:
            if block not in previous.code_blocks:
                previous.code_blocks.append(block)
        if not previous.timestamp:
            previous.timestamp = message.timestamp
        if not previous.model:
            previous.model = message.model
        if not previous.message_id:
            previous.message_id = message.message_id
        if message.metadata:
            previous.metadata.update(message.metadata)
        if not previous.code_blocks and not previous.texte:
            # message vide absorbe : on garde le precedent
            pass
    return merged


@dataclass
class CodeBlock:
    """Bloc de code structure extrait d'un message."""

    language: str = ""
    code: str = ""

    def __post_init__(self) -> None:
        self.language = "" if self.language is None else str(self.language).strip()
        self.code = "" if self.code is None else str(self.code)

    def to_dict(self) -> Dict[str, str]:
        return {"language": self.language, "code": self.code}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CodeBlock":
        return cls(language=data.get("language", ""), code=data.get("code", ""))


@dataclass
class Message:
    """Message standardise : role, texte, code_blocks, metadonnees d'identification."""

    role: str
    texte: str = ""
    conversation_id: str = ""
    message_id: str = ""
    platform: str = ""
    model: Optional[str] = None
    timestamp: Optional[str] = None
    code_blocks: List[CodeBlock] = field(default_factory=list)
    #: extras internes (tokens, sources, had_thinking...) non exportes
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.texte = "" if self.texte is None else str(self.texte)
        self.timestamp = normalize_timestamp(self.timestamp)
        self.model = str(self.model) if self.model else None
        self.message_id = "" if self.message_id is None else str(self.message_id)
        self.conversation_id = (
            "" if self.conversation_id is None else str(self.conversation_id)
        )
        self.platform = "" if self.platform is None else str(self.platform)
        converted: List[CodeBlock] = []
        for block in self.code_blocks or []:
            converted.append(
                block if isinstance(block, CodeBlock) else CodeBlock.from_dict(block)
            )
        # le texte conserve le markdown complet ; code_blocks en est la vue structuree
        self.code_blocks = converted or extract_code_blocks(self.texte)
        if self.metadata is None:
            self.metadata = {}

    @property
    def has_code(self) -> bool:
        return bool(self.code_blocks)

    def validate(self) -> None:
        if self.role not in VALID_ROLES:
            raise SchemaError(
                f"role invalide {self.role!r} (attendu: {', '.join(VALID_ROLES)})"
            )
        if not isinstance(self.texte, str):
            raise SchemaError("texte doit etre une chaine")
        if not isinstance(self.metadata, dict):
            raise SchemaError("metadata doit etre un objet")
        if self.timestamp is not None and parse_iso(self.timestamp) is None:
            raise SchemaError(f"timestamp invalide: {self.timestamp!r}")

    def to_dict(self) -> Dict[str, Any]:
        """Representation standardisee (8 champs, sans metadata interne)."""
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "role": self.role,
            "platform": self.platform,
            "model": self.model,
            "timestamp": self.timestamp,
            "texte": self.texte,
            "code_blocks": [b.to_dict() for b in self.code_blocks],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        try:
            msg = cls(
                role=data["role"],
                texte=data.get("texte", data.get("content", "")),
                conversation_id=data.get("conversation_id", ""),
                message_id=data.get("message_id", ""),
                platform=data.get("platform", ""),
                model=data.get("model") or (data.get("metadata") or {}).get("model"),
                timestamp=normalize_timestamp(data.get("timestamp")),
                code_blocks=[
                    CodeBlock.from_dict(b) for b in (data.get("code_blocks") or [])
                ],
                metadata=dict(data.get("metadata") or {}),
            )
        except KeyError as exc:
            raise SchemaError(f"message incomplet: champ {exc} manquant") from exc
        msg.validate()
        return msg


@dataclass
class Conversation:
    conversation_id: str
    platform: str
    title: str = ""
    messages: List[Message] = field(default_factory=list)
    model: Optional[str] = None
    started_at: Optional[str] = None
    last_message_at: Optional[str] = None
    exported_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.messages = [
            m if isinstance(m, Message) else Message.from_dict(m) for m in self.messages
        ]
        for msg in self.messages:
            if not msg.conversation_id:
                msg.conversation_id = self.conversation_id
            if not msg.platform:
                msg.platform = self.platform
        self.started_at = normalize_timestamp(self.started_at)
        self.last_message_at = normalize_timestamp(self.last_message_at)
        self.exported_at = normalize_timestamp(self.exported_at)

    def derive_timestamps(self) -> None:
        """started_at / last_message_at depuis les messages quand absents."""
        stamps = [m.timestamp for m in self.messages if m.timestamp]
        if stamps:
            if not self.started_at:
                self.started_at = min(stamps, key=lambda s: parse_iso(s))
            if not self.last_message_at:
                self.last_message_at = max(stamps, key=lambda s: parse_iso(s))
        if not self.exported_at:
            self.exported_at = to_iso_z(utc_now())

    def validate(self) -> None:
        if not self.platform or not isinstance(self.platform, str):
            raise SchemaError("platform requis (chaine non vide)")
        if not self.conversation_id:
            raise SchemaError("conversation_id requis")
        if not isinstance(self.title, str):
            raise SchemaError("title doit etre une chaine")
        if not isinstance(self.messages, list) or not self.messages:
            raise SchemaError("messages requis (liste non vide)")
        for i, msg in enumerate(self.messages):
            try:
                msg.validate()
            except SchemaError as exc:
                raise SchemaError(f"message[{i}]: {exc}") from exc
        if not self.started_at and self.messages and self.messages[0].timestamp:
            raise SchemaError("started_at incoherent avec le premier message")

    @property
    def has_code(self) -> bool:
        return any(m.has_code for m in self.messages)

    def to_dict(self, validate: bool = True) -> Dict[str, Any]:
        if validate:
            self.validate()
        return {
            "conversation_id": self.conversation_id,
            "platform": self.platform,
            "title": self.title,
            "model": self.model,
            "started_at": self.started_at,
            "last_message_at": self.last_message_at,
            "exported_at": self.exported_at,
            "messages": [m.to_dict() for m in self.messages],
        }

    def to_json(self, indent: int = 2, validate: bool = True) -> str:
        return json.dumps(self.to_dict(validate=validate), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], validate: bool = True) -> "Conversation":
        try:
            conv = cls(
                conversation_id=data["conversation_id"],
                platform=data.get("platform", data.get("service", "")),
                title=data.get("title", ""),
                messages=[Message.from_dict(m) for m in data.get("messages", [])],
                model=data.get("model"),
                started_at=normalize_timestamp(data.get("started_at")),
                last_message_at=normalize_timestamp(data.get("last_message_at")),
                exported_at=normalize_timestamp(data.get("exported_at")),
            )
        except KeyError as exc:
            raise SchemaError(f"conversation incomplete: champ {exc} manquant") from exc
        if validate:
            conv.validate()
        return conv

    @classmethod
    def from_json(cls, text: str) -> "Conversation":
        return cls.from_dict(json.loads(text))


@dataclass
class ConversationRef:
    """Reference vers une conversation trouvee dans une sidebar (avant scraping).

    Pas parte du JSON exporte : objet interne de decouverte/listing.
    """

    service: str
    id: str
    url: str
    title: Optional[str] = None
    section: Optional[str] = None  # "Today", "Yesterday", "Previous 7 Days"...
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        self.url = str(self.url).strip()
        if self.raw is None:
            self.raw = {}


def build_conversation(
    platform: str,
    conversation_id: str,
    title: str,
    messages: Iterable[Message],
    model: Optional[str] = None,
) -> Conversation:
    """Fabrique une conversation valide avec timestamps derives et exported_at."""
    conv = Conversation(
        platform=platform,
        conversation_id=conversation_id,
        title=title or "",
        messages=list(messages),
        model=model,
    )
    conv.derive_timestamps()
    conv.validate()
    return conv
