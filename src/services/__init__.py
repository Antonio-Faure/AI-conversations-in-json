"""Services de scraping par plateforme."""

from .base import BaseService, ScrapedPage, ServiceNotLoggedIn
from .chatgpt import ChatGPTService
from .claude import ClaudeService
from .gemini import GeminiService
from .grok import GrokService
from .mistral import MistralService
from .perplexity import PerplexityService

SERVICE_CLASSES: dict[str, type[BaseService]] = {
    "chatgpt": ChatGPTService,
    "claude": ClaudeService,
    "gemini": GeminiService,
    "perplexity": PerplexityService,
    "grok": GrokService,
    "mistral": MistralService,
}

#: plateformes scrapees pour l'instant (toutes actives)
ACTIVE_SERVICES: tuple[str, ...] = (
    "chatgpt", "claude", "gemini", "perplexity", "grok", "mistral",
)


__all__ = [
    "BaseService",
    "ScrapedPage",
    "ServiceNotLoggedIn",
    "ChatGPTService",
    "ClaudeService",
    "GeminiService",
    "PerplexityService",
    "GrokService",
    "MistralService",
    "SERVICE_CLASSES",
    "ACTIVE_SERVICES",
]
