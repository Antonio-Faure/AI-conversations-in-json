"""Driver Grok (interface web ; lecture via API, envoi via navigateur)."""

from __future__ import annotations

from .base import ChatDriver


class GrokDriver(ChatDriver):
    name = "grok"
    home_url = "https://grok.com/"

    new_chat_selectors = (
        "a[href='/chat']",
        "a[href='/chat/new']",
        "button[aria-label*='New']",
        "button[aria-label*='Nouveau']",
    )
    input_selectors = (
        "div[contenteditable='true']",
        "textarea",
        "form [contenteditable='true']",
    )
    send_selectors = (
        "button[type='submit']",
        "button[aria-label*='Submit']",
        "button[aria-label*='Send']",
        "button[aria-label*='Envoyer']",
    )
    file_input_selectors = ("input[type='file']",)
    stop_selectors = (
        "button[aria-label*='Stop']",
        "button[aria-label*='Arreter']",
        "button[aria-label*='Arretez']",
    )
    assistant_selectors = ("div.message-bubble", "[data-testid*='message']")
    rate_limit_markers = (
        "rate limit",
        "too many requests",
        "try again later",
        "reessayez plus tard",
        "réessayez plus tard",
        "limite atteinte",
    )
