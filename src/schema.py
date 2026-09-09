"""Schema JSON standardise pour les conversations IA.

Format commun a toutes les plateformes (ChatGPT, Claude, Gemini, Perplexity):

{
  "service": "chatgpt",
  "conversation_id": "abc123",
  "title": "...",
  "started_at": "2025-01-15T10:30:00Z",
  "last_message_at": "...",
  "model": "gpt-4",
  "exported_at": "...",
  "messages": [
    {"role": "user", "content": "...", "timestamp": "...", "metadata": {}}
  ]
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

VALID_ROLES = ("user", "assistant", "system", "tool")
KNOWN_SERVICES = ("chatgpt", "claude", "gemini", "perplexity")


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


@dataclass
class Message:
    role: str
    content: str
    timestamp: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.content = "" if self.content is None else str(self.content)
        self.timestamp = normalize_timestamp(self.timestamp)
        if self.metadata is None:
            self.metadata = {}

    def validate(self) -> None:
        if self.role not in VALID_ROLES:
            raise SchemaError(
                f"role invalide {self.role!r} (attendu: {', '.join(VALID_ROLES)})"
            )
        if not isinstance(self.content, str):
            raise SchemaError("content doit etre une chaine")
        if not isinstance(self.metadata, dict):
            raise SchemaError("metadata doit etre un objet")
        if self.timestamp is not None and parse_iso(self.timestamp) is None:
            raise SchemaError(f"timestamp invalide: {self.timestamp!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        try:
            msg = cls(
                role=data["role"],
                content=data.get("content", ""),
                timestamp=normalize_timestamp(data.get("timestamp")),
                metadata=dict(data.get("metadata") or {}),
            )
        except KeyError as exc:
            raise SchemaError(f"message incomplet: champ {exc} manquant") from exc
        msg.validate()
        return msg


@dataclass
class Conversation:
    service: str
    conversation_id: str
    title: str
    messages: List[Message] = field(default_factory=list)
    started_at: Optional[str] = None
    last_message_at: Optional[str] = None
    model: Optional[str] = None
    exported_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.messages = [
            m if isinstance(m, Message) else Message.from_dict(m) for m in self.messages
        ]
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
        if not self.service or not isinstance(self.service, str):
            raise SchemaError("service requis (chaine non vide)")
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

    def to_dict(self, validate: bool = True) -> Dict[str, Any]:
        if validate:
            self.validate()
        return {
            "service": self.service,
            "conversation_id": self.conversation_id,
            "title": self.title,
            "started_at": self.started_at,
            "last_message_at": self.last_message_at,
            "model": self.model,
            "exported_at": self.exported_at,
            "messages": [m.to_dict() for m in self.messages],
        }

    def to_json(self, indent: int = 2, validate: bool = True) -> str:
        return json.dumps(self.to_dict(validate=validate), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], validate: bool = True) -> "Conversation":
        try:
            conv = cls(
                service=data["service"],
                conversation_id=data["conversation_id"],
                title=data.get("title", ""),
                messages=[Message.from_dict(m) for m in data.get("messages", [])],
                started_at=normalize_timestamp(data.get("started_at")),
                last_message_at=normalize_timestamp(data.get("last_message_at")),
                model=data.get("model"),
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
    service: str,
    conversation_id: str,
    title: str,
    messages: Iterable[Message],
    model: Optional[str] = None,
) -> Conversation:
    """Fabrique une conversation valide avec timestamps derives et exported_at."""
    conv = Conversation(
        service=service,
        conversation_id=conversation_id,
        title=title or "",
        messages=list(messages),
        model=model,
    )
    conv.derive_timestamps()
    conv.validate()
    return conv
