"""Driver ChatGPT (envoi de messages et de pieces jointes)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

from .base import ChatDriver

#: Nombre de caracteres compares entre le message envoye et le dernier tour
#: utilisateur rendu dans le DOM.
_PROBE_LEN = 40


def _norm_text(value: str) -> str:
    """Normalise un texte pour comparer le message envoye au tour rendu."""
    return re.sub(r"\s+", " ", value or "").strip().lower()


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
        # limite specifique des conversations avec fichiers/images (free tier)
        "include files or images",
        "chat paused until",
        "start a new text-only chat",
        "limit for chats",
        "message limit",
        "rate limit",
        "limite de messages",
        "trop de messages",
        "reessayez plus tard",
        "réessayez plus tard",
        "upgrade to continue",
    )

    # -- navigation -----------------------------------------------------------

    def open_conversation(self, url: str) -> None:
        """Ouvre une conversation et attend l'hydratation du champ de saisie."""
        super().open_conversation(url)
        self._wait_for_input()

    def attach(self, paths: Sequence[Path | str]) -> bool:
        """Joint des fichiers puis laisse le composeur traiter la piece jointe.

        Sans cette pause, le clic d'envoi peut etre avale pendant le rendu de
        la piece jointe : le message reste dans le champ (deja observe sur
        `document.pdf`).
        """
        ok = super().attach(paths)
        if ok:
            self.session.wait_ms(1500)
        return ok

    def send(self, text: str) -> bool:
        """Attend le champ, envoie, puis confirme que le champ s'est vide.

        ChatGPT peut ignorer le clic d'envoi si une piece jointe est encore en
        cours de traitement : le message reste alors dans le champ. On attend
        donc que le champ se vide et, si besoin, on retente l'envoi une fois.
        """
        self._pending_text = text or ""
        if text and not self._wait_for_input():
            return False
        if not super().send(text):
            return False
        if self._wait_input_cleared():
            return True
        self.session.click_any(list(self.send_selectors), 4000)
        return self._wait_input_cleared()

    def confirm_sent(self) -> bool:
        """Confirme que le message est bien inscrit dans le fil.

        Le champ vide ne suffit pas : en cas de rate-limit ChatGPT peut vider
        le composeur puis restaurer le brouillon, ou l'ignorer, sans poster le
        message (constate en etalonnage : des envois refuses etaient comptes).
        On verifie donc aussi que le dernier tour utilisateur correspond au
        texte envoye.
        """
        if not super().confirm_sent():
            return False
        expected = (getattr(self, "_pending_text", "") or "").strip()
        if not expected:
            return True
        if self.is_rate_limited():
            return False
        if self._last_user_matches(expected):
            return True
        for _ in range(10):
            self.session.wait_ms(500)
            if self._last_user_matches(expected):
                return True
            if self.is_rate_limited():
                return False
        return False

    def _field_empty(self) -> bool:
        """Vrai si le premier champ de saisie trouve est vide."""
        checker = getattr(self.session, "is_input_empty", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(list(self.input_selectors)))
        except Exception:  # noqa: BLE001
            return True

    def _wait_input_cleared(self, timeout_ms: int = 8000) -> bool:
        """Attend que le champ de saisie soit vide (clic d'envoi pris en compte)."""
        elapsed = 0
        step = 250
        while elapsed < timeout_ms:
            if self._field_empty():
                return True
            self.session.wait_ms(step)
            elapsed += step
        return self._field_empty()

    def _last_user_text(self) -> str:
        """Texte du dernier tour utilisateur rendu (dernier message envoye)."""
        body = (
            "const els = document.querySelectorAll("
            "\"[data-message-author-role='user']\");"
            "if (!els.length) return '';"
            "return els[els.length - 1].innerText || '';"
        )
        try:
            return str(self.session.eval_body(body) or "")
        except Exception:  # noqa: BLE001
            return ""

    def _last_user_matches(self, expected: str) -> bool:
        """Vrai si le dernier tour utilisateur correspond au message envoye.

        Le tour rendu peut prefixer le texte par la piece jointe
        (`audio.mp3\\nFile\\n...`) ou le tronquer : on cherche donc le debut du
        message dans le tour, et on retente sur un prefixe plus court.
        """
        actual = _norm_text(self._last_user_text())
        if not actual:
            return False
        wanted = _norm_text(expected)
        for size in (_PROBE_LEN, 20):
            probe = wanted[:size]
            if probe and probe in actual:
                return True
        return False

    def _wait_for_input(self, timeout_ms: int = 20000) -> bool:
        """Attend que le contenteditable de saisie soit monte (React)."""
        elapsed = 0
        step = 500
        while elapsed < timeout_ms:
            if any(
                self.session.is_element_present(sel)
                for sel in self.input_selectors
            ):
                return True
            self.session.wait_ms(step)
            elapsed += step
        return False

    def new_conversation(self) -> bool:
        """Ouvre une nouvelle conversation ChatGPT.

        La page d'accueil est deja une conversation vierge ; on clique en plus
        le bouton « nouveau chat » visible. Le DOM contient un doublon cache
        de `create-new-chat-button`, que `page.click` viserait en premier (d'ou
        un timeout) : on selectionne donc explicitement l'element visible via
        JS. L'URL reste `https://chatgpt.com/` jusqu'au premier message.
        """
        self.open_home()
        self.session.wait_ms(2000)
        self._click_visible_new_chat()
        for _ in range(20):
            if self.session.is_element_present(self.input_selectors[0]):
                return True
            self.session.wait_ms(500)
        return self.session.is_element_present(self.input_selectors[0])

    def _click_visible_new_chat(self) -> bool:
        """Clique le premier bouton « nouveau chat » reellement visible."""
        selectors = json.dumps(list(self.new_chat_selectors))
        body = (
            "const sels = %s;"
            "for (const s of sels) {"
            "  const els = [...document.querySelectorAll(s)];"
            "  for (const el of els) {"
            "    const r = el.getBoundingClientRect();"
            "    const st = getComputedStyle(el);"
            "    if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden'"
            "        && st.display !== 'none') { el.click(); return s; }"
            "  }"
            "}"
            "return '';"
        ) % selectors
        try:
            return bool(self.session.eval_body(body))
        except Exception:  # noqa: BLE001
            return False
