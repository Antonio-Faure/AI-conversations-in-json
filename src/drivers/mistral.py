"""Driver Mistral (envoi de messages et de pieces jointes ; modes work/chat).

Mistral expose deux modes (`/work` et `/chat`) qui sont deux applications
distinctes : passer l'URL de la conversation cible (`--url`) est la facon
fiable de reprendre un etalon dans le bon mode.
"""

from __future__ import annotations

from .base import ChatDriver


class MistralDriver(ChatDriver):
    name = "mistral"
    home_url = "https://chat.mistral.ai/work"

    new_chat_selectors = (
        "a[href*='/work']",
        "a[href*='/chat']",
        "button[aria-label*='New']",
        "button[aria-label*='Nouvelle']",
    )
    input_selectors = (
        "div[contenteditable='true']",
        "textarea",
        "form textarea",
    )
    send_selectors = (
        "button[type='submit']",
        "button[aria-label*='Send']",
        "button[aria-label*='Envoyer']",
    )
    file_input_selectors = ("input[type='file']",)
    stop_selectors = (
        "button[aria-label*='Stop']",
        "button[aria-label*='Arreter']",
    )
    assistant_selectors = (
        "[data-message-author-role='assistant']",
        "[data-message-author-role]",
    )
    rate_limit_markers = (
        "rate limit",
        "too many requests",
        "reessayez plus tard",
        "réessayez plus tard",
        "limite de requetes",
        "limite de requêtes",
        "quota",
    )
