"""Driver Perplexity (envoi de messages et de pieces jointes)."""

from __future__ import annotations

from .base import ChatDriver


class PerplexityDriver(ChatDriver):
    name = "perplexity"
    home_url = "https://www.perplexity.ai/"

    new_chat_selectors = (
        "a[href='/']",
        "button[aria-label*='New']",
        "button[aria-label*='Nouveau']",
    )
    input_selectors = (
        "textarea[placeholder]",
        "div[contenteditable='true']",
        "textarea",
    )
    send_selectors = (
        "button[aria-label*='Submit']",
        "button[aria-label*='Envoyer']",
        "button[aria-label*='Send']",
        "button[type='submit']",
    )
    file_input_selectors = ("input[type='file']",)
    stop_selectors = (
        "button[aria-label*='Stop']",
        "button[aria-label*='Arreter']",
    )
    assistant_selectors = (
        "div[data-testid='answer']",
        "div[class*='prose']",
    )
    rate_limit_markers = (
        "rate limit",
        "too many requests",
        "out of searches",
        "reessayez plus tard",
        "réessayez plus tard",
        "try again later",
        "limite",
    )
