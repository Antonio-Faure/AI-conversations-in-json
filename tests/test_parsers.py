"""Tests des parsers DOM sur fixtures HTML (sans navigateur)."""

from __future__ import annotations

import pytest

from src.parsers import (
    ChatGPTParser,
    ClaudeParser,
    GeminiParser,
    PARSER_CLASSES,
    PerplexityParser,
)
from src.parsers.base import ParseError
from src.schema import Conversation


# ---------------------------------------------------------------------------
# Listes de conversations (sidebars)
# ---------------------------------------------------------------------------


class TestParseLinks:
    def test_chatgpt_sidebar(self, fixture_html):
        refs = ChatGPTParser().parse_links(fixture_html("chatgpt_home.html"), "https://chatgpt.com/")
        assert [r.id for r in refs] == [
            "2f1e9c3a-7b45-4d8e-9a11-0c2d3e4f5a6b",
            "88a07f6e-5d4c-3b2a-1908-f7e6d5c4b3a2",
            "deadbeefcafe0123456789abcdef0123456789",
        ]
        assert refs[0].title == "Refactor module paiement"
        assert refs[0].url.startswith("https://")  # absolutisee
        assert refs[0].service == "chatgpt"

    def test_claude_sidebar(self, fixture_html):
        refs = ClaudeParser().parse_links(fixture_html("claude_home.html"), "https://claude.ai/chats")
        assert len(refs) == 2
        assert all("/chat/" in r.url for r in refs)

    def test_gemini_sidebar_ignore_settings(self, fixture_html):
        refs = GeminiParser().parse_links(fixture_html("gemini_home.html"), "https://gemini.google.com/app")
        ids = [r.id for r in refs]
        assert "1a2b3c4d5e6f7g8h9i0j12kl34" in ids
        assert all("settings" not in r.url for r in refs)

    def test_perplexity_sidebar_ignore_library(self, fixture_html):
        refs = PerplexityParser().parse_links(
            fixture_html("perplexity_home.html"), "https://www.perplexity.ai/"
        )
        assert len(refs) == 2
        assert all(r.id.startswith("ou-en") or r.id.startswith("meilleurs") for r in refs)


# ---------------------------------------------------------------------------
# Conversations completes
# ---------------------------------------------------------------------------


class TestChatGPTParser:
    def test_structure_complete(self, fixture_html):
        conv = ChatGPTParser().parse(
            fixture_html("chatgpt_conversation.html"), conversation_id="cid-42"
        )
        assert isinstance(conv, Conversation)
        assert conv.service == "chatgpt"
        assert conv.conversation_id == "cid-42"
        assert conv.title == "Refactor module paiement"
        assert conv.model == "gpt-5"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_markdown_et_nettoyage(self, fixture_html):
        conv = ChatGPTParser().parse(fixture_html("chatgpt_conversation.html"), conversation_id="c")
        first = conv.messages[0]
        assert "refactorer mon module de paiement" in first.content
        assert "\n" in conv.messages[1].content  # liste separee en lignes
        assert "Copier" not in conv.messages[1].content  # boutons exclus
        assert "color:red" not in conv.messages[3].content  # style exclu
        assert "```python" in conv.messages[3].content  # bloc code fence

    def test_timestamps_via_meta_react(self, fixture_html):
        extra = {
            "messages": {
                "aa11bb22-0001-4334-9556-778899aabbcc": {"time": 1767522600, "model": None},
                "aa11bb22-0002-4334-9556-778899aabbcc": {"time": 1767522630, "model": "gpt-5"},
            }
        }
        conv = ChatGPTParser().parse(
            fixture_html("chatgpt_conversation.html"), conversation_id="c", extra=extra
        )
        assert conv.messages[0].timestamp == "2026-01-04T10:30:00Z"
        assert conv.started_at == "2026-01-04T10:30:00Z"
        assert conv.messages[1].metadata["model"] == "gpt-5"

    def test_sans_messages_erreur(self, fixture_html):
        with pytest.raises(ParseError):
            ChatGPTParser().parse("<html><body><main></main></body></html>")


class TestClaudeParser:
    def test_structure_complete(self, fixture_html):
        conv = ClaudeParser().parse(
            fixture_html("claude_conversation.html"),
            conversation_id="6f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
        )
        assert conv.service == "claude"
        assert conv.title == "Préparer un entretien technique"
        assert conv.model == "Claude Sonnet 4"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_thinking_exclu_et_signale(self, fixture_html):
        conv = ClaudeParser().parse(fixture_html("claude_conversation.html"), conversation_id="c")
        assistant = conv.messages[1]
        assert "organiser 7 jours" not in assistant.content  # reflexion retiree
        assert assistant.metadata.get("had_thinking") is True

    def test_timestamps_et_code(self, fixture_html):
        conv = ClaudeParser().parse(fixture_html("claude_conversation.html"), conversation_id="c")
        assert conv.messages[1].timestamp == "2026-09-03T14:05:00Z"
        assert conv.started_at == "2026-09-03T14:05:00Z"
        assert conv.last_message_at == "2026-09-03T14:12:30Z"
        assert "```bash" in conv.messages[3].content
        assert conv.messages[0].timestamp is None  # pas de time dans le parent direct


class TestGeminiParser:
    def test_structure_complete(self, fixture_html):
        conv = GeminiParser().parse(
            fixture_html("gemini_conversation.html"), conversation_id="cid-gemini"
        )
        assert conv.service == "gemini"
        assert conv.title == "Plan de voyage Japon"
        assert conv.model == "Gemini 2.5 Pro"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_contenu_et_ordre(self, fixture_html):
        conv = GeminiParser().parse(fixture_html("gemini_conversation.html"), conversation_id="c")
        assert "10 jours au Japon" in conv.messages[0].content
        assert "Tokyo 3j" in conv.messages[1].content
        assert "JR Pass" in conv.messages[3].content


class TestPerplexityParser:
    def test_structure_complete(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="cid-perp"
        )
        assert conv.service == "perplexity"
        assert conv.title.startswith("Où en sont les voitures")
        assert conv.model == "sonar-pro"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_sources_dans_metadata(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="c"
        )
        sources = conv.messages[1].metadata["sources"]
        assert sources == ["aceee.org", "eea.europa.eu"]

    def test_started_at_depuis_en_tete(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="c"
        )
        assert conv.started_at == "2026-09-05T08:00:00Z"


class TestTousLesParsers:
    @pytest.mark.parametrize("service", sorted(PARSER_CLASSES))
    def test_export_valide_et_serialisable(self, service, fixture_html):
        parser = PARSER_CLASSES[service]()
        conv = parser.parse(
            fixture_html(f"{service}_conversation.html"), conversation_id=f"cid-{service}"
        )
        conv.validate()
        data = conv.to_dict()
        assert data["service"] == service
        assert all(m["metadata"] is not None for m in data["messages"])
        import json

        json.loads(json.dumps(data))  # roundtrip sans exception
