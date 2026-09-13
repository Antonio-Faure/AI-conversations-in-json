"""Classe de base des drivers de conversation.

Un driver sait, pour une plateforme donnee :
  - ouvrir une nouvelle conversation ou une conversation existante ;
  - saisir un message et l'envoyer ;
  - joindre des fichiers ;
  - attendre la fin de la reponse ;
  - detecter un rate-limit.

Les selecteurs sont des chaines de repli (le DOM des plateformes bouge). Les
methodes s'appuient uniquement sur la facade `session` (Playwright ou
Botasaurus), jamais sur `session.page`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..utils.logging import get_logger, log_fields

log = get_logger("aicv.drivers")


class ChatDriver:
    name = ""
    home_url = ""

    #: boutons/liens de creation d'une nouvelle conversation
    new_chat_selectors: Sequence[str] = ()
    #: champs de saisie du message (contenteditable ou textarea)
    input_selectors: Sequence[str] = ()
    #: boutons d'envoi
    send_selectors: Sequence[str] = ()
    #: `<input type=file>` (souvent cache)
    file_input_selectors: Sequence[str] = ()
    #: bouton "stop"/"arret" visible pendant la generation
    stop_selectors: Sequence[str] = ()
    #: conteneurs des messages assistant
    assistant_selectors: Sequence[str] = ()
    #: fragments de texte signalant un rate-limit
    rate_limit_markers: Sequence[str] = ()

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        self.session = session
        self.config = config or {}
        self.timeout_ms = int(self.config.get("timeout_ms", 45000))
        self.pacing_ms = int(self.config.get("etalon_pacing_ms", 4000))

    # -- navigation -----------------------------------------------------------

    def open_home(self) -> None:
        self.session.goto(self.home_url)

    def open_conversation(self, url: str) -> None:
        self.session.goto(url)

    def new_conversation(self) -> bool:
        self.open_home()
        clicked = self.session.click_any(list(self.new_chat_selectors), 6000)
        self.session.wait_ms(1500)
        return clicked is not None

    # -- lecture d'etat -------------------------------------------------------

    def page_text(self) -> str:
        body = self.session.eval_body(
            "return document.body ? document.body.innerText : '';"
        )
        return str(body or "")

    def is_rate_limited(self) -> bool:
        text = self.page_text().lower()
        for marker in self.rate_limit_markers:
            if marker.lower() in text:
                return True
        return False

    def is_generating(self) -> bool:
        return any(
            self.session.is_element_present(selector)
            for selector in self.stop_selectors
        )

    def count_assistant_messages(self) -> int:
        if not self.assistant_selectors:
            return -1
        import json

        body = (
            "const sels = %s; const set = new Set();"
            "for (const s of sels) { try { document.querySelectorAll(s)"
            ".forEach(function (n) { set.add(n); }); } catch (e) {} }"
            "return set.size;" % json.dumps(list(self.assistant_selectors))
        )
        try:
            return int(self.session.eval_body(body) or 0)
        except (TypeError, ValueError):
            return -1

    # -- actions --------------------------------------------------------------

    def attach(self, paths: Sequence[Path | str]) -> bool:
        files = [path for path in paths if path]
        if not files:
            return True
        return self.session.upload_any(list(self.file_input_selectors), files) is not None

    def send(self, text: str) -> bool:
        if text:
            if self.session.type_into(list(self.input_selectors), text) is None:
                return False
            self.session.wait_ms(300)
        if self.session.click_any(list(self.send_selectors), 4000) is None:
            self.session.press("Enter")
        return True

    def confirm_sent(self) -> bool:
        """Verifie que l'envoi a bien ete accepte (champ de saisie vide).

        Empeche de compter un message non parti (bouton rate/erreur). Si le
        moteur ne sait pas verifier, on suppose l'envoi accepte.
        """
        checker = getattr(self.session, "is_input_empty", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(list(self.input_selectors)))
        except Exception:  # noqa: BLE001
            return True

    def wait_for_response(self, timeout_ms: Optional[int] = None) -> bool:
        """Attend la fin de la generation (disparition du bouton stop)."""
        timeout = int(timeout_ms or self.timeout_ms)
        step = 500
        elapsed = 0
        # laisser le temps au bouton stop d'apparaitre
        while elapsed < timeout and not self.is_generating():
            self.session.wait_ms(step)
            elapsed += step
        while elapsed < timeout and self.is_generating():
            self.session.wait_ms(step)
            elapsed += step
        self.session.wait_ms(1000)
        return not self.is_generating()

    def pause(self) -> None:
        if self.pacing_ms > 0:
            self.session.wait_ms(self.pacing_ms)

    def log_progress(self, index: int, total: int, extra: Optional[dict] = None) -> None:
        log_fields(
            log, 20, f"{self.name}: message {index}/{total}",
            extra={"driver": self.name, **(extra or {})},
        )
