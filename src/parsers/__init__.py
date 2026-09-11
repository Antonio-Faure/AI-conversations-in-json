"""Parsers pour chaque plateforme IA."""

from .base import BaseParser, ParseError
from .chatgpt import ChatGPTParser
from .claude import ClaudeParser
from .gemini import GeminiParser
from .grok import GrokParser
from .mistral import MistralParser
from .perplexity import PerplexityParser

PARSER_CLASSES = {
    "chatgpt": ChatGPTParser,
    "claude": ClaudeParser,
    "gemini": GeminiParser,
    "perplexity": PerplexityParser,
    "grok": GrokParser,
    "mistral": MistralParser,
}

__all__ = [
    "BaseParser",
    "ParseError",
    "ChatGPTParser",
    "ClaudeParser",
    "GeminiParser",
    "PerplexityParser",
    "GrokParser",
    "MistralParser",
    "PARSER_CLASSES",
]
