"""Driver Gemini (envoi de messages et de pieces jointes)."""

from __future__ import annotations

from .base import ChatDriver


class GeminiDriver(ChatDriver):
    name = "gemini"
    home_url = "https://gemini.google.com/app"

    new_chat_selectors = (
        "button[aria-label*='Nouvelle conversation']",
        "button[aria-label*='New chat']",
        "a[href='/app']",
        "side-navigation button[aria-label*='New']",
    )
    input_selectors = (
        "rich-textarea .ql-editor[contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "rich-textarea [contenteditable='true']",
        "[contenteditable='true'][role='textbox']",
        "textarea",
    )
    send_selectors = (
        "button.send-button",
        "button[aria-label*='Envoyer']",
        "button[aria-label*='Send message']",
        "button[aria-label*='Send']",
    )
    file_input_selectors = ("input[type='file']",)
    stop_selectors = (
        "button[aria-label*='Arreter']",
        "button[aria-label*='Arretez']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Interrompre']",
    )
    assistant_selectors = ("model-response", "model-response message-content")
    rate_limit_markers = (
        "rate limit",
        "limite de messages",
        "trop de requetes",
        "trop de requêtes",
        "reessayez plus tard",
        "réessayez plus tard",
        "you've reached",
        "try again later",
        "quota",
    )
