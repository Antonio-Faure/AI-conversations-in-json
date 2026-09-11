"""Tests du schema JSON standardise (src/schema.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.schema import (
    CodeBlock,
    Conversation,
    ConversationRef,
    Message,
    SchemaError,
    build_conversation,
    extract_code_blocks,
    normalize_timestamp,
    parse_iso,
    to_iso_z,
)


class TestTimestamps:
    def test_parse_iso_accepte_z(self):
        dt = parse_iso("2026-01-15T10:30:00Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 1 and dt.hour == 10
        assert dt.tzinfo is not None

    def test_parse_iso_accepte_offset_et_normalise_utc(self):
        dt = parse_iso("2026-01-15T12:30:00+02:00")
        assert dt is not None
        assert dt.astimezone(timezone.utc).hour == 10

    def test_parse_iso_epoch(self):
        assert parse_iso(1767580200) is not None

    @pytest.mark.parametrize("bad", ["", None, "pas une date", "9:41 PM", "15/01/2026"])
    def test_parse_iso_rejette_bruits(self, bad):
        assert parse_iso(bad) is None

    def test_normalize_timestamp_en_iso_z(self):
        assert normalize_timestamp("2026-01-15T12:30:00+02:00") == "2026-01-15T10:30:00Z"
        assert normalize_timestamp(None) is None
        assert normalize_timestamp("garbage") is None

    def test_to_iso_z(self):
        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert to_iso_z(dt) == "2026-01-15T10:30:00Z"


class TestCodeBlocks:
    def test_extrait_langage_et_code(self):
        text = "avant\n```python\nprint(1)\n```\napres"
        blocks = extract_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0].language == "python"
        assert blocks[0].code == "print(1)"

    def test_fence_sans_langage(self):
        blocks = extract_code_blocks("```\nls -la\n```")
        assert blocks[0].language == ""
        assert blocks[0].code == "ls -la"

    def test_plusieurs_blocs_ordre(self):
        text = "```bash\na\n```\ntexte\n```python\nb\n```"
        langs = [b.language for b in extract_code_blocks(text)]
        assert langs == ["bash", "python"]

    def test_to_dict(self):
        assert CodeBlock("python", "x=1").to_dict() == {"language": "python", "code": "x=1"}


class TestMessage:
    def test_message_valide(self):
        msg = Message(role="user", texte="bonjour", timestamp="2026-01-15T10:30:00Z")
        msg.validate()
        assert msg.timestamp == "2026-01-15T10:30:00Z"

    def test_role_invalide_rejete(self):
        msg = Message(role="robot", texte="x")
        with pytest.raises(SchemaError, match="role invalide"):
            msg.validate()

    def test_texte_none_devient_chaine_vide(self):
        msg = Message(role="user", texte=None)
        assert msg.texte == ""

    def test_timestamp_normalise_a_la_construction(self):
        msg = Message(role="user", texte="x", timestamp="2026-01-15T11:30:00+01:00")
        assert msg.timestamp == "2026-01-15T10:30:00Z"

    def test_code_blocks_derives_du_texte(self):
        msg = Message(role="assistant", texte="```python\nprint(1)\n```")
        assert msg.has_code
        assert msg.code_blocks[0].language == "python"
        # le texte conserve le markdown integral
        assert "```python" in msg.texte

    def test_to_dict_standard_huit_champs(self):
        msg = Message(
            role="assistant",
            texte="ok",
            conversation_id="c1",
            message_id="m1",
            platform="chatgpt",
            model="gpt-4",
            timestamp="2026-01-15T10:30:05Z",
        )
        data = msg.to_dict()
        assert list(data.keys()) == [
            "conversation_id",
            "message_id",
            "role",
            "platform",
            "model",
            "timestamp",
            "texte",
            "code_blocks",
        ]
        assert data["conversation_id"] == "c1"
        assert data["message_id"] == "m1"
        assert data["platform"] == "chatgpt"
        assert "metadata" not in data

    def test_from_dict_roundtrip(self):
        msg = Message(
            role="assistant", texte="x", conversation_id="c", platform="chatgpt",
            model="gpt-4", timestamp="2026-01-15T10:30:05Z",
        )
        assert Message.from_dict(msg.to_dict()) == msg

    def test_from_dict_champ_manquant(self):
        with pytest.raises(SchemaError):
            Message.from_dict({"texte": "x"})


class TestConversation:
    @pytest.fixture()
    def conversation(self):
        return build_conversation(
            platform="chatgpt",
            conversation_id="abc123",
            title="Test",
            messages=[
                Message("user", "q", timestamp="2026-01-15T10:30:00Z"),
                Message("assistant", "r", timestamp="2026-01-15T11:45:00Z"),
            ],
            model="gpt-4",
        )

    def test_json_standard_formate_plan(self, conversation):
        data = conversation.to_dict()
        assert list(data.keys()) == [
            "conversation_id", "platform", "title", "model",
            "started_at", "last_message_at", "exported_at", "messages",
        ]
        assert data["platform"] == "chatgpt"
        assert data["started_at"] == "2026-01-15T10:30:00Z"
        assert data["last_message_at"] == "2026-01-15T11:45:00Z"
        assert data["exported_at"]  # derivee automatiquement
        json.dumps(data)  # serialisable

    def test_messages_recus_les_metadonnees_conversation(self, conversation):
        for msg in conversation.messages:
            assert msg.conversation_id == "abc123"
            assert msg.platform == "chatgpt"

    def test_derive_timestamps_min_max(self):
        conv = Conversation(
            platform="claude",
            conversation_id="x",
            title="t",
            messages=[
                Message("user", "a", timestamp="2026-03-05T09:00:00Z"),
                Message("assistant", "b", timestamp="2026-03-01T08:00:00Z"),
            ],
        )
        conv.derive_timestamps()
        assert conv.started_at == "2026-03-01T08:00:00Z"
        assert conv.last_message_at == "2026-03-05T09:00:00Z"

    def test_messages_vides_rejetes(self, conversation):
        conversation.messages = []
        with pytest.raises(SchemaError, match="messages"):
            conversation.validate()

    def test_platform_vide_rejetee(self, conversation):
        conversation.platform = ""
        with pytest.raises(SchemaError):
            conversation.validate()

    def test_has_code(self, conversation):
        assert conversation.has_code is False
        conversation.messages[1].texte = "```python\nx\n```"
        conversation.messages[1].code_blocks = extract_code_blocks(conversation.messages[1].texte)
        assert conversation.has_code is True

    def test_roundtrip_json(self, conversation):
        text = conversation.to_json()
        restored = Conversation.from_json(text)
        assert restored.to_dict() == conversation.to_dict()


class TestConversationRef:
    def test_ref_strip_et_defaults(self):
        ref = ConversationRef(service="chatgpt", id=" abc ", url=" https://x/c/abc ")
        assert ref.id == "abc"
        assert ref.url == "https://x/c/abc"
        assert ref.raw == {}
