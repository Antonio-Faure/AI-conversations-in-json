"""Tests du schema JSON standardise (src/schema.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.schema import (
    Conversation,
    ConversationRef,
    Message,
    SchemaError,
    build_conversation,
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
        dt = parse_iso(1767580200)
        assert dt is not None

    @pytest.mark.parametrize("bad", ["", None, "pas une date", "9:41 PM", "15/01/2026"])
    def test_parse_iso_rejette_bruits(self, bad):
        assert parse_iso(bad) is None

    def test_parse_iso_epoch_entier(self):
        assert parse_iso(1767580200) is not None

    def test_normalize_timestamp_en_iso_z(self):
        assert normalize_timestamp("2026-01-15T12:30:00+02:00") == "2026-01-15T10:30:00Z"
        assert normalize_timestamp(None) is None
        assert normalize_timestamp("garbage") is None

    def test_to_iso_z(self):
        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        assert to_iso_z(dt) == "2026-01-15T10:30:00Z"


class TestMessage:
    def test_message_valide(self):
        msg = Message(role="user", content="bonjour", timestamp="2026-01-15T10:30:00Z")
        msg.validate()
        assert msg.timestamp == "2026-01-15T10:30:00Z"

    def test_role_invalide_rejete(self):
        msg = Message(role="robot", content="x")
        with pytest.raises(SchemaError, match="role invalide"):
            msg.validate()

    def test_content_none_devient_chaine_vide(self):
        msg = Message(role="user", content=None)
        assert msg.content == ""

    def test_timestamp_normalise_a_la_construction(self):
        msg = Message(role="user", content="x", timestamp="2026-01-15T11:30:00+01:00")
        assert msg.timestamp == "2026-01-15T10:30:00Z"

    def test_to_dict_from_dict_roundtrip(self):
        msg = Message(
            role="assistant",
            content="ok",
            timestamp="2026-01-15T10:30:05Z",
            metadata={"model": "gpt-4", "tokens": None},
        )
        data = msg.to_dict()
        assert set(data) == {"role", "content", "timestamp", "metadata"}
        assert Message.from_dict(data) == msg

    def test_from_dict_champ_manquant(self):
        with pytest.raises(SchemaError):
            Message.from_dict({"content": "x"})


class TestConversation:
    @pytest.fixture()
    def conversation(self):
        return build_conversation(
            service="chatgpt",
            conversation_id="abc123",
            title="Test",
            messages=[
                Message("user", "q", "2026-01-15T10:30:00Z"),
                Message("assistant", "r", "2026-01-15T11:45:00Z", {"model": "gpt-4", "tokens": None}),
            ],
            model="gpt-4",
        )

    def test_json_standard_formate_plan(self, conversation):
        data = conversation.to_dict()
        assert list(data.keys()) == [
            "service",
            "conversation_id",
            "title",
            "started_at",
            "last_message_at",
            "model",
            "exported_at",
            "messages",
        ]
        assert data["service"] == "chatgpt"
        assert data["started_at"] == "2026-01-15T10:30:00Z"
        assert data["last_message_at"] == "2026-01-15T11:45:00Z"
        assert data["model"] == "gpt-4"
        assert data["exported_at"]  # derivee automatiquement
        json.dumps(data)  # serialisable

    def test_derive_timestamps_min_max(self, conversation):
        conv = Conversation(
            service="claude",
            conversation_id="x",
            title="t",
            messages=[
                Message("user", "a", "2026-03-05T09:00:00Z"),
                Message("assistant", "b", "2026-03-01T08:00:00Z"),
            ],
        )
        conv.derive_timestamps()
        # min/max chronologiques, pas ordre d'apparition
        assert conv.started_at == "2026-03-01T08:00:00Z"
        assert conv.last_message_at == "2026-03-05T09:00:00Z"

    def test_messages_vides_rejetes(self, conversation):
        conversation.messages = []
        with pytest.raises(SchemaError, match="messages"):
            conversation.validate()

    def test_service_vide_rejete(self, conversation):
        conversation.service = ""
        with pytest.raises(SchemaError):
            conversation.validate()

    def test_roundtrip_json(self, conversation):
        text = conversation.to_json()
        restored = Conversation.from_json(text)
        assert restored.to_dict() == conversation.to_dict()

    def test_from_dict_dict_messages_convertis(self, conversation):
        data = conversation.to_dict()
        data["messages"][0] = dict(data["messages"][0])  # deja dict
        conv = Conversation.from_dict(data)
        assert all(isinstance(m, Message) for m in conv.messages)


class TestConversationRef:
    def test_ref_strip_et_defaults(self):
        ref = ConversationRef(service="chatgpt", id=" abc ", url=" https://x/c/abc ")
        assert ref.id == "abc"
        assert ref.url == "https://x/c/abc"
        assert ref.raw == {}
