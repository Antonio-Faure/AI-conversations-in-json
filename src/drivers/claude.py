"""Driver Claude (envoi de messages et de pieces jointes)."""

from __future__ import annotations

import json
import logging

from .base import ChatDriver

log = logging.getLogger("aicv.drivers.claude")


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
    #: Marqueurs specifiques de quota epuise. Les formulations trop larges
    #: (« try again » seul, « you've reached ») apparaissent dans les toasts
    #: d'erreur transitoires de claude.ai et declenchaient un faux rate-limit
    #: apres un envoi (constate en etalonnage, message 135). On ne garde que
    #: les tournures reelles de limite de messages.
    rate_limit_markers = (
        "message limit",
        "reached your message limit",
        "reached your limit",
        "out of messages",
        "rate limit",
        "too many requests",
        "limit will reset",
        "limit resets",
        "try again later",
        "try again at",
        "limite de messages",
        "limite de message",
        "reessayez plus tard",
        "réessayez plus tard",
    )

    # -- etat -----------------------------------------------------------------

    def is_rate_limited(self) -> bool:
        """Detecte un quota epuise et journalise le marqueur exact.

        Le log du marqueur + du contexte permet de diagnostiquer les faux
        positifs (bannieres de mise en garde transitoires) sans deviner.
        """
        text = self.page_text().lower()
        for marker in self.rate_limit_markers:
            index = text.find(marker.lower())
            if index != -1:
                snippet = text[max(0, index - 100):index + 150].replace("\n", " ")
                log.warning("claude: marqueur rate-limit %r -> %r", marker, snippet)
                return True
        return False

    # -- envoi ----------------------------------------------------------------

    def _input_is_empty(self) -> bool:
        """True si le champ de saisie est vide (message bien soumis).

        En cas d'echec d'evaluation on renvoie False : mieux vaut considerer
        l'envoi comme non abouti que compter un message jamais parti.
        """
        body = (
            "const sels = %s;"
            "for (const s of sels) { const el = document.querySelector(s);"
            "  if (!el) continue;"
            "  const v = (el.value !== undefined && el.value !== null)"
            "    ? el.value : (el.innerText || '');"
            "  return String(v).trim().length === 0;"
            "}"
            "return null;"
        ) % json.dumps(list(self.input_selectors))
        try:
            res = self.session.eval_body(body)
        except Exception:  # noqa: BLE001
            return False
        return res is True

    def _clear_input(self) -> None:
        """Vide le champ de saisie (brouillon residuel d'un envoi refuse)."""
        body = (
            "const el = document.querySelector(\"div.ProseMirror[contenteditable='true']\");"
            "if (!el) return false;"
            "el.focus();"
            "const sel = window.getSelection();"
            "const range = document.createRange();"
            "range.selectNodeContents(el);"
            "sel.removeAllRanges(); sel.addRange(range);"
            "document.execCommand('delete');"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
            "return true;"
        )
        try:
            self.session.eval_body(body)
        except Exception:  # noqa: BLE001
            pass

    def send(self, text: str) -> bool:
        """Saisit puis envoie, en verifiant que le champ reste vide.

        En botasaurus `press('Enter')` est un no-op : si le bouton d'envoi est
        absent/desactive (limite atteinte), le message resterait dans le champ.
        Claude peut aussi vider le champ un instant avant de restaurer le
        brouillon quand l'envoi est refuse : on reverifie apres une pause pour
        ne pas compter un message jamais parti comme envoye.
        """
        if text:
            # purge d'un eventuel brouillon (envoi precedent refuse)
            if not self._input_is_empty():
                self._clear_input()
                self.session.wait_ms(500)
                if not self._input_is_empty():
                    log.warning("claude: champ non vide, envoi annule")
                    return False
            if self.session.type_into(list(self.input_selectors), text) is None:
                return False
            self.session.wait_ms(300)
        if self.session.click_any(list(self.send_selectors), 4000) is None:
            self.session.press("Enter")
        # 1) le champ doit se vider (envoi soumis)
        cleared = False
        for _ in range(16):
            if self._input_is_empty():
                cleared = True
                break
            self.session.wait_ms(500)
        if not cleared:
            return False
        # 2) un envoi refuse (limite) restaure le brouillon juste apres
        self.session.wait_ms(2500)
        return self._input_is_empty()
