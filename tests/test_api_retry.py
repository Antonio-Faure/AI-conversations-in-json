"""Tests transverses du plafond `Retry-After` des clients API (audit F10).

Un en-tete `Retry-After` de plusieurs heures ne doit pas endormir le run : les
cinq clients API plafonnent l'attente a leur `API_RETRY_WAIT_MAX_S` (300 s).
"""

from __future__ import annotations

import time

import pytest

from src.services.chatgpt import API_RETRY_WAIT_MAX_S as CHATGPT_WAIT_MAX_S
from src.services.chatgpt import _ChatGPTHttpClient
from src.services.claude import API_RETRY_WAIT_MAX_S as CLAUDE_WAIT_MAX_S
from src.services.claude import _ClaudeHttpClient
from src.services.gemini import API_RETRY_WAIT_MAX_S as GEMINI_WAIT_MAX_S
from src.services.gemini import _GeminiHttpClient
from src.services.mistral import API_RETRY_WAIT_MAX_S as MISTRAL_WAIT_MAX_S
from src.services.mistral import _MistralHttpClient
from src.services.perplexity import API_RETRY_WAIT_MAX_S as PERPLEXITY_WAIT_MAX_S
from src.services.perplexity import _PerplexityHttpClient

#: (fabrique de client, plafond attendu) pour les cinq clients API
CASES = [
    pytest.param(lambda: _ChatGPTHttpClient("a=b"), CHATGPT_WAIT_MAX_S, id="chatgpt"),
    pytest.param(lambda: _ClaudeHttpClient("a=b"), CLAUDE_WAIT_MAX_S, id="claude"),
    pytest.param(
        lambda: _GeminiHttpClient([], {}, user_agent="UA-test"),
        GEMINI_WAIT_MAX_S,
        id="gemini",
    ),
    pytest.param(lambda: _MistralHttpClient("a=b"), MISTRAL_WAIT_MAX_S, id="mistral"),
    pytest.param(
        lambda: _PerplexityHttpClient("a=b"), PERPLEXITY_WAIT_MAX_S, id="perplexity"
    ),
]


@pytest.mark.parametrize("factory,max_s", CASES)
def test_retry_after_plafonne(factory, max_s, monkeypatch):
    """`Retry-After: 86400` -> attente plafonnee, pas 24 h par essai."""
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = factory()
    assert client._wait_429(0, {"Retry-After": "86400"}, "https://exemple/x") is True
    assert sleeps == [float(max_s)]


@pytest.mark.parametrize("factory,max_s", CASES)
def test_retry_after_raisonnable_respecte(factory, max_s, monkeypatch):
    """Un `Retry-After` court reste respecte tel quel (pas de plancher ajoute)."""
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = factory()
    assert client._wait_429(0, {"Retry-After": "12"}, "https://exemple/x") is True
    assert sleeps == [12.0]
