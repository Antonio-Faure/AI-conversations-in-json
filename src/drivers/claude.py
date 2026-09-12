"""Driver Claude (envoi de messages et de pieces jointes)."""

from __future__ import annotations

from .base import ChatDriver


class ClaudeDriver(ChatDriver):
    name = "claude"
    home_url = "https://claude.ai/new"

    new_chat_selectors = (
        "[data-testid='new-chat-button']",
        "a[href='/new']",
        "button[aria-label*='New chat']",
        "button[aria-label*='Nouvelle conversation']",
    )
    input_selectors = (
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true'][data-placeholder]",
        "div[contenteditable='true']",
    )
    send_selectors = (
        "button[aria-label='Send message']",
        "button[aria-label*='Send']",
        "button[aria-label*='Envoyer']",
        "button[type='submit']",
    )
    file_input_selectors = ("input[type='file']",)
    stop_selectors = (
        "button[aria-label*='Stop']",
        "button[aria-label*='Arreter']",
    )
    assistant_selectors = (
        "div[data-testid='assistant-message-text']",
        "div.font-claude-response",
        "div.font-claude-message",
    )
    rate_limit_markers = (
        "message limit",
        "you've reached",
        "you have reached",
        "rate limit",
        "limite de messages",
        "reessayez plus tard",
        "réessayez plus tard",
        "try again",
    )
