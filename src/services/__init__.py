"""Services de scraping par plateforme."""

from .base import BaseService, ScrapedPage, ServiceNotLoggedIn
from .chatgpt import ChatGPTService
from .claude import ClaudeService
from .gemini import GeminiService
from .perplexity import PerplexityService

SERVICE_CLASSES: dict[str, type[BaseService]] = {
    "chatgpt": ChatGPTService,
    "claude": ClaudeService,
    "gemini": GeminiService,
    "perplexity": PerplexityService,
}

SERVICE_REGISTRY = SERVICE_CLASSES


def get_service_class(name: str) -> type[BaseService]:
    """Retourne la classe du service ou leve KeyError."""
    return SERVICE_CLASSES[name]


__all__ = [
    "BaseService",
    "ScrapedPage",
    "ServiceNotLoggedIn",
    "ChatGPTService",
    "ClaudeService",
    "GeminiService",
    "PerplexityService",
    "SERVICE_CLASSES",
    "SERVICE_REGISTRY",
    "get_service_class",
]
