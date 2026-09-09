"""Parsers pour chaque plateforme IA."""

from .base import BaseParser, ParseError
from .chatgpt import ChatGPTParser
from .claude import ClaudeParser
from .gemini import GeminiParser
from .perplexity import PerplexityParser

PARSER_CLASSES = {
    "chatgpt": ChatGPTParser,
    "claude": ClaudeParser,
    "gemini": GeminiParser,
    "perplexity": PerplexityParser,
}

__all__ = [
    "BaseParser",
    "ParseError",
    "ChatGPTParser",
    "ClaudeParser",
    "GeminiParser",
    "PerplexityParser",
    "PARSER_CLASSES",
]
