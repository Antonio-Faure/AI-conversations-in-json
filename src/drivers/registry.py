"""Registre des drivers de conversation disponibles."""

from __future__ import annotations

from typing import Dict, List, Type

from .base import ChatDriver
from .chatgpt import ChatGPTDriver
from .claude import ClaudeDriver
from .gemini import GeminiDriver
from .grok import GrokDriver
from .mistral import MistralDriver
from .perplexity import PerplexityDriver

DRIVERS: Dict[str, Type[ChatDriver]] = {
    ChatGPTDriver.name: ChatGPTDriver,
    ClaudeDriver.name: ClaudeDriver,
    GeminiDriver.name: GeminiDriver,
    GrokDriver.name: GrokDriver,
    MistralDriver.name: MistralDriver,
    PerplexityDriver.name: PerplexityDriver,
}


def get_driver(name: str) -> Type[ChatDriver]:
    try:
        return DRIVERS[name]
    except KeyError as exc:
        raise KeyError(
            f"driver inconnu {name!r} (disponibles: {', '.join(available())})"
        ) from exc


def available() -> List[str]:
    return sorted(DRIVERS)
