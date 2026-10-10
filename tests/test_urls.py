"""Tests des URLs de conversation (src/utils/urls.py)."""

from __future__ import annotations

from src.utils.urls import (
    canonical_url_from_html,
    conversation_url,
    normalize_conversation_url,
)


def test_url_par_plateforme():
    assert conversation_url("chatgpt", "abc") == "https://chatgpt.com/c/abc"
    assert conversation_url("claude", "abc") == "https://claude.ai/chat/abc"
    assert conversation_url("gemini", "abc") == "https://gemini.google.com/app/abc"
    assert conversation_url("perplexity", "abc") == "https://www.perplexity.ai/search/abc"
    assert conversation_url("grok", "abc") == "https://grok.com/chat/abc"


def test_url_mistral_work_uniquement():
    assert conversation_url("mistral", "abc") == "https://chat.mistral.ai/work/abc"
    # `mode` est obsolete (fusion /chat -> /work) et ignore
    assert conversation_url("mistral", "abc", "chat") == "https://chat.mistral.ai/work/abc"


def test_normalize_conversation_url_mistral():
    assert normalize_conversation_url(
        "https://chat.mistral.ai/chat/abc"
    ) == "https://chat.mistral.ai/work/abc"
    assert normalize_conversation_url(
        "https://chat.mistral.ai/chat/abc?x=1"
    ) == "https://chat.mistral.ai/work/abc?x=1"
    assert normalize_conversation_url(
        "https://chat.mistral.ai/work/abc"
    ) == "https://chat.mistral.ai/work/abc"
    assert normalize_conversation_url("https://chatgpt.com/c/abc") == "https://chatgpt.com/c/abc"
    assert normalize_conversation_url("") == ""


def test_url_inconnue_ou_vide():
    assert conversation_url("inconnu", "abc") == ""
    assert conversation_url("grok", "") == ""


def test_canonical_url_from_html():
    html = '<html><head><link rel="canonical" href="https://chat.mistral.ai/work/xyz">'
    assert canonical_url_from_html(html) == "https://chat.mistral.ai/work/xyz"
    assert canonical_url_from_html("<html></html>") is None
