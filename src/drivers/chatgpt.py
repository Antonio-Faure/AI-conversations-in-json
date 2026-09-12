"""Driver ChatGPT (envoi de messages et de pieces jointes)."""

from __future__ import annotations

from .base import ChatDriver


class ChatGPTDriver(ChatDriver):
    name = "chatgpt"
    home_url = "https://chatgpt.com/"

    new_chat_selectors = (
        "[data-testid='create-new-chat-button']",
        "a[href='/']",
        "button[aria-label*='New chat']",
        "button[aria-label*='Nouveau chat']",
        "button[aria-label*='Nouvelle conversation']",
    )
    input_selectors = (
        "div#prompt-textarea[contenteditable='true']",
        "#prompt-textarea",
        "div[contenteditable='true'][id='prompt-textarea']",
        "textarea#prompt-textarea",
        "form [contenteditable='true']",
    )
    send_selectors = (
        "[data-testid='send-button']",
        "button[aria-label*='Send prompt']",
        "button[aria-label*='Envoyer']",
        "button[aria-label*='Send']",
    )
    file_input_selectors = ("input[type='file']",)
    stop_selectors = (
        "[data-testid='stop-button']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Arreter']",
    )
    assistant_selectors = (
        "[data-message-author-role='assistant']",
        "[data-testid^='conversation-turn']",
    )
    rate_limit_markers = (
        "you've reached",
        "you have reached",
        "message limit",
        "rate limit",
        "limite de messages",
        "trop de messages",
        "reessayez plus tard",
        "réessayez plus tard",
        "upgrade to continue",
    )
