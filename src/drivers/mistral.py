"""Driver Mistral (envoi de messages et de pieces jointes ; modes work/chat).

Mistral expose deux modes (`/work` et `/chat`) qui sont deux applications
distinctes ; la navigation directe vers `/chat` est redirigee vers `/work`
selon le dernier mode utilise. On force donc le mode via les liens de l'« app
switcher » (`a[href='/chat']`, `a[href='/work']`) avant d'ouvrir la
conversation cible. Passer l'URL de la conversation (`--url`) reste la facon
fiable de reprendre un etalon dans le bon mode.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from .base import ChatDriver

log = logging.getLogger("aicv.drivers.mistral")


class MistralDriver(ChatDriver):
    name = "mistral"
    home_url = "https://chat.mistral.ai/work"

    #: liens de l'app switcher pour forcer le mode (chat|work)
    mode_selectors = {
        "chat": "a[href='/chat']",
        "work": "a[href='/work']",
    }
    new_chat_selectors = (
        "button[aria-label*='Nouveau']",
        "button[aria-label*='New']",
        "a[href='/chat']",
        "a[href='/work']",
    )
    input_selectors = (
        "div[contenteditable='true']",
        "textarea",
        "form textarea",
    )
    send_selectors = (
        "button[aria-label*='Envoyer']",
        "button[aria-label*='Send']",
        "button[type='submit']",
    )
    file_input_selectors = ("input[type='file']",)
    #: bouton « + » du compositeur qui ouvre le menu d'import (le champ fichier
    #: n'est monte dans le DOM qu'apres ouverture de ce menu)
    attach_menu_selectors = (
        "button[aria-label='Ouvrir le menu des paramètres']",
        "button[aria-label*='Ouvrir le menu']",
        "button[aria-label*='Joindre']",
        "button[aria-label*='Ajouter']",
    )
    stop_selectors = (
        "button[aria-label*='Stop']",
        "button[aria-label*='Arreter']",
    )
    assistant_selectors = (
        "[data-message-author-role='assistant']",
        "[data-message-author-role]",
    )
    #: Marqueurs de quota epuise. On evite les formulations trop larges
    #: (« quota ») qui apparaissent dans des reponses normales et declenchent
    #: un faux rate-limit. La banniere reelle est « Limite de messages
    #: atteinte », eventuellement suivie de « réinitialisée dans ... ».
    rate_limit_markers = (
        "rate limit",
        "too many requests",
        "reessayez plus tard",
        "réessayez plus tard",
        "limite de requetes",
        "limite de requêtes",
        "limite de messages",
        "limite de messages atteinte",
        "limite atteinte",
        "réinitialisée dans",
        "reinitialisee dans",
    )

    def __init__(self, session, config=None):
        super().__init__(session, config)
        self._mode = None
        # etat capture juste avant l'envoi, pour detecter la reponse meme si
        # elle apparait pendant la saisie/le clic
        self._pre_count = 0
        self._pre_len = -1

    @staticmethod
    def mode_of(url: str) -> "str | None":
        """Deduit le mode (chat|work) porte par le chemin de l'URL.

        On parse le chemin : le domaine `chat.mistral.ai` contient lui-meme
        `/chat`, une recherche de sous-chaine serait donc trompeuse.
        """
        if not url:
            return None
        segments = urlparse(url).path.strip("/").split("/")
        first = segments[0] if segments and segments[0] else None
        return first if first in ("chat", "work") else None

    def ensure_mode(self, mode: str) -> None:
        """Force le mode demande via l'app switcher (le shell par defaut est /work)."""
        if mode not in self.mode_selectors or self._mode == mode:
            return
        self.session.goto(self.home_url)
        self.session.wait_ms(3000)
        if mode != "work":
            selector = self.mode_selectors[mode]
            clicked = self.session.eval_body(
                "const a=document.querySelector(%s);"
                "if(a){a.click();return true}return false;" % json.dumps(selector)
            )
            if clicked:
                self.session.wait_ms(4000)
        self._mode = mode

    def is_rate_limited(self) -> bool:
        """Detecte un quota epuise et journalise le marqueur exact.

        Le log du marqueur + du contexte permet de diagnostiquer les faux
        positifs (reponses mentionnant un quota) sans deviner.
        """
        text = self.page_text().lower()
        for marker in self.rate_limit_markers:
            index = text.find(marker.lower())
            if index != -1:
                snippet = text[max(0, index - 100):index + 150].replace("\n", " ")
                log.warning("mistral: marqueur rate-limit %r -> %r", marker, snippet)
                return True
        return False

    def open_home(self) -> None:
        self.ensure_mode("work")

    def open_conversation(self, url: str) -> None:
        mode = self.mode_of(url)
        if mode:
            self.ensure_mode(mode)
        self.session.goto(url)
        self.session.wait_ms(1500)

    def new_conversation(self) -> bool:
        self.ensure_mode("work")
        return super().new_conversation()

    # -- pieces jointes -------------------------------------------------------

    def attach(self, paths) -> bool:
        """Joint des fichiers : ouvre le menu « + » qui monte le champ fichier."""
        files = [p for p in paths if p]
        if not files:
            return True
        if not self.session.is_element_present("input[type='file']"):
            self.session.click_any(list(self.attach_menu_selectors), 5000)
            self.session.wait_ms(1200)
        uploaded = self.session.upload_any(list(self.file_input_selectors), files)
        if uploaded is None:
            return False
        # laisser l'upload se terminer et fermer le menu eventuel (Echap)
        self.session.wait_ms(2000)
        try:
            self.session.eval_body(
                "document.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Escape',bubbles:true}));return true;"
            )
        except Exception:  # noqa: BLE001
            pass
        return True

    # -- attente de reponse ---------------------------------------------------

    def _assistant_count(self) -> int:
        """Nombre de tours assistant presents dans le DOM."""
        try:
            return int(
                self.session.eval_body(
                    "return document.querySelectorAll("
                    "\"[data-message-author-role='assistant']\").length;"
                )
                or 0
            )
        except (TypeError, ValueError):
            return -1

    def _last_assistant_len(self) -> int:
        """Longueur du dernier message assistant (pour detecter la stabilisation)."""
        body = (
            "const els=document.querySelectorAll("
            "\"[data-message-author-role='assistant']\");"
            "if(!els.length) return -1;"
            "return (els[els.length-1].innerText||'').length;"
        )
        try:
            return int(self.session.eval_body(body) or -1)
        except (TypeError, ValueError):
            return -1

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
        return res is True or str(res).strip().lower() == "true"

    def _clear_input(self) -> None:
        """Vide le champ de saisie (brouillon residuel d'un envoi refuse)."""
        body = (
            "const el = document.querySelector(\"div[contenteditable='true']\");"
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
        """Saisit puis envoie via le bouton visible « Envoyer ».

        Le compositeur contient aussi un `<button type="submit" hidden>` : le
        clic doit viser le bouton visible, sinon le message reste dans le champ.

        Un envoi refuse (limite atteinte) peut vider le champ un instant puis
        restaurer le brouillon : on reverifie donc le champ apres une pause pour
        ne pas compter un message jamais parti comme envoye.
        """
        # capture l'etat assistant AVANT la saisie : une reponse rapide peut
        # deja etre presente quand wait_for_response demarre.
        self._pre_count = self._assistant_count()
        self._pre_len = self._last_assistant_len()
        if text:
            # purge d'un eventuel brouillon (envoi precedent refuse)
            if not self._input_is_empty():
                self._clear_input()
                self.session.wait_ms(500)
                if not self._input_is_empty():
                    log.warning("mistral: champ non vide, envoi annule")
                    return False
            if self.session.type_into(list(self.input_selectors), text) is None:
                return False
            self.session.wait_ms(400)

        clicked = self.session.eval_body(
            "const bs=[...document.querySelectorAll("
            "\"button[aria-label='Envoyer'],button[aria-label*='Envoyer'],"
            "button[aria-label*='Send']\")];"
            "const b=bs.find(x=>{const r=x.getBoundingClientRect();"
            "return r.width>0&&r.height>0&&!x.disabled;})"
            "||bs.find(x=>!x.disabled);"
            "if(b){b.click();return true}return false;"
        )
        if not clicked:
            self.session.eval_body(
                "const ce=document.querySelector(\"div[contenteditable='true']\");"
                "if(ce){ce.focus();ce.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));"
                "return true}return false;"
            )

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

    def wait_for_response(self, timeout_ms=None) -> bool:
        """Attend la fin de generation.

        Le DOM Mistral n'expose pas toujours un bouton « stop » : on combine
        l'apparition d'un nouveau tour assistant et la stabilisation de son
        texte, sinon on retombe sur le comportement de base.
        """
        timeout = int(timeout_ms or self.timeout_ms)
        step = 500
        elapsed = 0
        before = self._pre_count
        before_len = self._pre_len

        # 1) attendre le debut de la reponse (stop visible, nouveau tour ou
        #    nouveau contenu dans le dernier tour assistant)
        while elapsed < timeout:
            self.session.wait_ms(step)
            elapsed += step
            if self.is_generating():
                break
            if self._assistant_count() > before:
                break
            if before_len >= 0 and self._last_assistant_len() != before_len:
                break

        # 2) attendre la fin : plus de generation et texte stable
        stable = 0
        last = -1
        while elapsed < timeout:
            generating = self.is_generating()
            current = self._last_assistant_len()
            if not generating and current > 0 and current == last:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = current
            self.session.wait_ms(step)
            elapsed += step
        self.session.wait_ms(500)
        return not self.is_generating()
