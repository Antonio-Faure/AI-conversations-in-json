"""Driver Grok (interface web ; lecture via API, envoi via navigateur).

Particularite de l'etalonnage : quand le quota gratuit est epuise, Grok insere
dans le fil une carte d'erreur « Free tier limit reached » qui **reste** dans le
transcript. Scanner tout le texte de la page ferait donc un faux positif a vie
sur une conversation reprise. On interroge donc l'etat live via l'API interne
`/rest/rate-limits` (`remainingQueries`) et on ne retombe sur les marqueurs DOM
qu'en dernier recours (API injoignable).
"""

from __future__ import annotations

import json

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
    # Modele interroge pour le quota (l'UI utilise « Rapide » = fast).
    rate_limit_model = "fast"
    # Grok affiche en francais "Limite levée dans X heures ... / passez a
    # SuperGrok". Marqueurs volontairement specifiques : un simple "limite"
    # matcherait les messages de test de l'etalon (ex. "limite quand x -> 0").
    # Attention : ces marqueurs peuvent aussi provenir d'une carte d'erreur
    # historique persistante -> ne servir que de repli si l'API echoue.
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

    # -- quota -----------------------------------------------------------------

    def remaining_queries(self) -> int | None:
        """Requetes restantes dans la fenetre glissante, ou None si indisponible.

        L'app expose `POST /rest/rate-limits` sur grok.com (meme origine que la
        page) : `{"windowSizeSeconds":86400,"remainingQueries":N,"totalQueries":30}`.
        """
        model = str(self.config.get("grok_rate_limit_model") or self.rate_limit_model)
        body = (
            "return (async function(){"
            "try {"
            "const r = await fetch('/rest/rate-limits', {method:'POST',"
            "credentials:'include',headers:{'content-type':'application/json'},"
            "body:JSON.stringify({modelName:%s})});"
            "if (!r.ok) return null;"
            "const d = await r.json();"
            "return (d && typeof d.remainingQueries === 'number')"
            " ? String(d.remainingQueries) : null;"
            "} catch (e) { return null; }"
            "})();" % json.dumps(model)
        )
        try:
            raw = self.session.eval_body(body)
        except Exception:  # noqa: BLE001
            return None
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def is_rate_limited(self) -> bool:
        remaining = self.remaining_queries()
        if remaining is not None:
            return remaining <= 0
        # Repli DOM (API injoignable) : potentiellement un faux positif si le
        # fil contient une carte de quota historique, mais on prefere s'arreter
        # plutot que d'envoyer dans le vide.
        return super().is_rate_limited()
