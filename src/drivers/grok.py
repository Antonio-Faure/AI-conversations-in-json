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
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true']",
        "form [contenteditable='true']",
        "textarea",
    )
    send_selectors = (
        "button[data-testid='chat-submit']",
        "button[type='submit']",
        "button[aria-label*='Envoyer']",
        "button[aria-label*='Send']",
        "button[aria-label*='Submit']",
    )
    file_input_selectors = ("input[type='file']",)
    # Le bouton d'envoi devient un bouton "stop" pendant la generation.
    stop_selectors = (
        "button[data-testid='chat-stop']",
        "button[aria-label*='Arrêter']",
        "button[aria-label*='Arreter']",
        "button[aria-label*='Stop']",
        "button[aria-label*='Cancel']",
        "button[aria-label*='Annuler']",
    )
    assistant_selectors = ("div.message-bubble", "[data-testid*='message']")
    # Grok affiche en francais "Limite levée dans X heures ... / passez a
    # SuperGrok". Marqueurs volontairement specifiques : un simple "limite"
    # matcherait les messages de test de l'etalon (ex. "limite quand x -> 0").
    rate_limit_markers = (
        "limite levée",
        "atteignez ou passez",
        "passez à supergrok",
        "upgrade to supergrok",
        "reach or upgrade",
        "rate limit",
        "too many requests",
        "try again later",
        "réessayez plus tard",
        "reessayez plus tard",
        "trop de requêtes",
        "limite atteinte",
    )
