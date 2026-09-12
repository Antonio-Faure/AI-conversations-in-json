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
    #: bouton qui ouvre le menu exposant le `<input type=file>` (sinon absent du DOM)
    tools_selectors = (
        "button[aria-label=\"Importation et outils\"]",
        "button[aria-label*='Importation']",
        "button[aria-label*='Tools']",
    )
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

    # -- pieces jointes -------------------------------------------------------

    def attach(self, paths) -> bool:
        """Joint des fichiers via le menu « Importation et outils ».

        Contrairement a la plupart des plateformes, Gemini n'expose pas de
        `<input type=file>` dans le DOM au repos : il n'apparait qu'apres avoir
        ouvert le menu outils. On l'ouvre donc a la demande, avec une seconde
        tentative si le menu s'est referme entre-temps.
        """
        files = [path for path in paths if path]
        if not files:
            return True
        if not self._open_file_input():
            return False
        if self.session.upload_any(list(self.file_input_selectors), files) is not None:
            return True
        self.session.wait_ms(1000)
        if not self._open_file_input(force=True):
            return False
        return self.session.upload_any(list(self.file_input_selectors), files) is not None

    def _open_file_input(self, force: bool = False) -> bool:
        """Ouvre le menu outils jusqu'a ce que le champ fichier soit present."""
        if not force and self.session.is_element_present(self.file_input_selectors[0]):
            return True
        if self.session.click_any(list(self.tools_selectors), 4000) is None:
            return False
        for _ in range(10):
            if self.session.is_element_present(self.file_input_selectors[0]):
                return True
            self.session.wait_ms(300)
        return False

