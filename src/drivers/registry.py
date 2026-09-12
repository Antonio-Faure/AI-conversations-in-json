"""Registre des drivers de conversation disponibles."""

from __future__ import annotations

from typing import Dict, List, Type

from .base import ChatDriver
from .gemini import GeminiDriver
from .grok import GrokDriver

DRIVERS: Dict[str, Type[ChatDriver]] = {
    GeminiDriver.name: GeminiDriver,
    GrokDriver.name: GrokDriver,
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
