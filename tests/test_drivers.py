"""Tests des drivers de conversation et du runner d'etalonnage (sans navigateur)."""

from __future__ import annotations

import json

import pytest

from src.drivers import get_driver
from src.drivers.base import ChatDriver
from src.drivers.runner import EtalonRunner, load_queue, load_state


class FakeSession:
    def __init__(self):
        self.url_value = "https://fake.test/chat/c1"
        self.sent = 0

    def goto(self, url):
        self.url_value = url

    def url(self):
        return self.url_value

    def wait_ms(self, ms):
        pass

    def eval_body(self, body):
        return ""

    def is_element_present(self, selector):
        return False

    def click_any(self, selectors, timeout_ms=0):
        return selectors[0] if selectors else None

    def type_into(self, selectors, text):
        self.sent += 1
        return selectors[0] if selectors else None

    def upload_any(self, selectors, paths):
        return selectors[0] if selectors else None

    def press(self, key):
        pass

    def screenshot(self, path):
        return None


class FakeDriver(ChatDriver):
    name = "fake"
    home_url = "https://fake.test/"
    new_chat_selectors = ("button.new",)
    input_selectors = ("div.input",)
    send_selectors = ("button.send",)
    file_input_selectors = ("input[type=file]",)
    stop_selectors = ("button.stop",)


class LimitedDriver(FakeDriver):
    def is_rate_limited(self):
        return self.session.sent >= 2


class NoUploadDriver(FakeDriver):
    def attach(self, paths):
        return False


def make_runner(tmp_path, driver, messages):
    driver.config["timeout_ms"] = 200
    driver.timeout_ms = 200
    return EtalonRunner(driver, tmp_path, messages, load_state(tmp_path / "state.json", "fake", tmp_path))


def test_queue_liste_ou_objet(tmp_path):
    p1 = tmp_path / "q1.json"
    p1.write_text(json.dumps([{"text": "a"}, "b"]), encoding="utf-8")
    assert load_queue(p1) == [{"text": "a"}, {"text": "b"}]
    p2 = tmp_path / "q2.json"
    p2.write_text(json.dumps({"messages": [{"text": "x"}]}), encoding="utf-8")
    assert load_queue(p2) == [{"text": "x"}]


def test_run_complete_et_etat(tmp_path):
    driver = FakeDriver(FakeSession())
    messages = [{"text": "m1"}, {"text": "m2"}, {"text": "m3"}]
    status = make_runner(tmp_path, driver, messages).run()
    assert status == "done"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["next_index"] == 3
    assert state["status"] == "done"
    assert state["target_url"]
    bilan = (tmp_path / "BILAN.md").read_text(encoding="utf-8")
    assert "done" in bilan and "3/3" in bilan


def test_reprise_sans_renvoyer(tmp_path):
    driver = FakeDriver(FakeSession())
    messages = [{"text": "m1"}, {"text": "m2"}]
    make_runner(tmp_path, driver, messages).run()
    # relance : deja termine -> aucun nouvel envoi
    driver2 = FakeDriver(FakeSession())
    state = load_state(tmp_path / "state.json", "fake", tmp_path)
    assert state.status == "done"
    status = EtalonRunner(driver2, tmp_path, messages, state).run()
    assert status == "done"
    assert driver2.session.sent == 0


def test_rate_limit_stoppe_et_reprend(tmp_path):
    driver = LimitedDriver(FakeSession())
    messages = [{"text": "m1"}, {"text": "m2"}, {"text": "m3"}]
    status = make_runner(tmp_path, driver, messages).run()
    assert status == "rate_limited"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["next_index"] == 2
    assert "rate" in state["status"]
    assert "Restants" in (tmp_path / "BILAN.md").read_text(encoding="utf-8")


def test_echec_upload(tmp_path):
    driver = NoUploadDriver(FakeSession())
    messages = [{"text": "m1", "attachments": ["image.png"]}]
    status = make_runner(tmp_path, driver, messages).run()
    assert status == "error"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["next_index"] == 0
    assert "upload" in (state["last_error"] or "")


class MenuSession(FakeSession):
    """Session ou le champ fichier n'existe qu'apres ouverture du menu outils."""

    def __init__(self):
        super().__init__()
        self.file_input_visible = False
        self.uploaded = []

    def is_element_present(self, selector):
        return self.file_input_visible and selector == "input[type='file']"

    def click_any(self, selectors, timeout_ms=0):
        for selector in selectors:
            if "Importation" in selector or "Tools" in selector:
                self.file_input_visible = True
                return selector
        return super().click_any(selectors, timeout_ms)

    def upload_any(self, selectors, paths):
        if not self.file_input_visible:
            return None
        self.uploaded.append(list(paths))
        return selectors[0] if selectors else None


class NoMenuSession(FakeSession):
    """Session sans menu outils : le champ fichier reste introuvable."""

    def is_element_present(self, selector):
        return False

    def click_any(self, selectors, timeout_ms=0):
        return None


def test_gemini_attach_ouvre_le_menu_outils(tmp_path):
    driver = get_driver("gemini")(MenuSession())
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"x")
    assert driver.attach([target]) is True
    assert driver.session.file_input_visible is True
    assert driver.session.uploaded == [[target]]


def test_gemini_attach_sans_menu_echoue(tmp_path):
    driver = get_driver("gemini")(NoMenuSession())
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"x")
    assert driver.attach([target]) is False


def test_gemini_attach_sans_fichier_ok():
    driver = get_driver("gemini")(MenuSession())
    assert driver.attach([]) is True


def test_target_url_recapture_apres_envoi(tmp_path):
    """Un nouveau chat n'a son id qu'apres le 1er message : l'URL doit suivre."""

    class LateSession(FakeSession):
        def type_into(self, selectors, text):
            result = super().type_into(selectors, text)
            self.url_value = "https://fake.test/c/xyz"
            return result

    driver = FakeDriver(LateSession())
    status = make_runner(tmp_path, driver, [{"text": "m1"}]).run()
    assert status == "done"
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["target_url"] == "https://fake.test/c/xyz"


def test_registry_drivers():
    assert get_driver("gemini").name == "gemini"
    assert get_driver("grok").name == "grok"
    with pytest.raises(KeyError):
        get_driver("inconnu")


def test_selecteurs_non_vides():
    for name in ("gemini", "grok"):
        cls = get_driver(name)
        assert cls.input_selectors and cls.send_selectors and cls.file_input_selectors


class TextSession(FakeSession):
    """Session factice dont le texte de page est pilote."""

    def __init__(self, text):
        super().__init__()
        self.text = text

    def eval_body(self, body):
        return self.text


def test_grok_selecteurs_cles():
    cls = get_driver("grok")
    assert "button[data-testid='chat-submit']" in cls.send_selectors
    assert "div.ProseMirror[contenteditable='true']" in cls.input_selectors
    assert "input[type='file']" in cls.file_input_selectors


class ChatGPTFakeSession(FakeSession):
    """Session factice ChatGPT : le champ de saisie n'apparait qu'apres N tests."""

    def __init__(self, input_after=1, click_returns=""):
        super().__init__()
        self.input_after = input_after
        self.click_returns = click_returns
        self.input_calls = 0
        self.eval_bodies = []
        self.typed = []

    def is_element_present(self, selector):
        if selector == "div#prompt-textarea[contenteditable='true']":
            self.input_calls += 1
            return self.input_calls >= self.input_after
        return False

    def eval_body(self, body):
        self.eval_bodies.append(body)
        return self.click_returns

    def type_into(self, selectors, text):
        self.typed.append(text)
        return selectors[0] if selectors else None


def test_chatgpt_new_chat_clique_le_bouton_visible():
    session = ChatGPTFakeSession(click_returns="clicked")
    driver = get_driver("chatgpt")(session)
    assert driver.new_conversation() is True
    assert session.url() == "https://chatgpt.com/"
    assert session.typed == []
    # le JS doit cibler le bouton « nouveau chat » et filtrer la visibilite
    assert any("create-new-chat-button" in b for b in session.eval_bodies)
    assert any("getBoundingClientRect" in b for b in session.eval_bodies)


def test_chatgpt_new_chat_sans_champ_echoue():
    session = ChatGPTFakeSession(input_after=10**9)
    driver = get_driver("chatgpt")(session)
    assert driver.new_conversation() is False


def test_chatgpt_send_attend_le_champ():
    session = ChatGPTFakeSession(input_after=3)
    driver = get_driver("chatgpt")(session)
    assert driver.send("bonjour") is True
    assert session.typed == ["bonjour"]


def test_chatgpt_send_sans_champ_echoue():
    session = ChatGPTFakeSession(input_after=10**9)
    driver = get_driver("chatgpt")(session)
    assert driver.send("bonjour") is False
    assert session.typed == []


class MistralModeSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.eval_bodies = []

    def eval_body(self, body):
        self.eval_bodies.append(body)
        return "clicked"


def test_mistral_mode_of():
    cls = get_driver("mistral")
    assert cls.mode_of("https://chat.mistral.ai/chat/abc") == "chat"
    assert cls.mode_of("https://chat.mistral.ai/work/abc") == "work"
    assert cls.mode_of("https://chat.mistral.ai/") is None


def test_mistral_open_conversation_force_le_mode_chat():
    session = MistralModeSession()
    driver = get_driver("mistral")(session)
    driver.open_conversation("https://chat.mistral.ai/chat")
    # le lien de l'app switcher chat doit avoir ete clique
    assert any("a[href='/chat']" in b for b in session.eval_bodies)
    assert session.url() == "https://chat.mistral.ai/chat"


def test_mistral_open_conversation_work_ne_bascule_pas():
    session = MistralModeSession()
    driver = get_driver("mistral")(session)
    driver.open_conversation("https://chat.mistral.ai/work")
    assert not any("a[href='/chat']" in b for b in session.eval_bodies)
    assert session.url() == "https://chat.mistral.ai/work"


class StreamingSession(FakeSession):
    """Session factice : reponse assistant qui grandit puis se stabilise."""

    def __init__(self):
        super().__init__()
        self.count_calls = 0
        self.length = 0

    def eval_body(self, body):
        if "els.length" in body:
            return str(self.length)
        if "querySelectorAll" in body:
            self.count_calls += 1
            return "0" if self.count_calls == 1 else "1"
        return ""

    def is_element_present(self, selector):
        return False

    def wait_ms(self, ms):
        if self.length < 10:
            self.length += 5


def test_mistral_wait_for_response_stabilisation():
    session = StreamingSession()
    driver = get_driver("mistral")(session)
    assert driver.wait_for_response(timeout_ms=20000) is True
    assert session.length == 10


class MistralAttachSession(FakeSession):
    """Session factice : le champ fichier n'apparait qu'apres le menu « + »."""

    def __init__(self):
        super().__init__()
        self.file_input_visible = False
        self.uploaded = []

    def is_element_present(self, selector):
        return self.file_input_visible and selector == "input[type='file']"

    def click_any(self, selectors, timeout_ms=0):
        for selector in selectors:
            if "Ouvrir le menu" in selector:
                self.file_input_visible = True
                return selector
        return None

    def upload_any(self, selectors, paths):
        if not self.file_input_visible:
            return None
        self.uploaded.append(list(paths))
        return selectors[0] if selectors else None

    def eval_body(self, body):
        return True


def test_mistral_attach_ouvre_le_menu(tmp_path):
    session = MistralAttachSession()
    driver = get_driver("mistral")(session)
    target = tmp_path / "image.png"
    target.write_bytes(b"x")
    assert driver.attach([target]) is True
    assert session.file_input_visible is True
    assert session.uploaded == [[target]]


def test_mistral_attach_sans_fichier_ok():
    assert get_driver("mistral")(MistralAttachSession()).attach([]) is True


def test_mistral_detecte_limite_messages():
    driver = get_driver("mistral")(
        TextSession("Limite de messages atteinte. Votre limite sera réinitialisée dans 54 minutes.")
    )
    assert driver.is_rate_limited() is True


def test_grok_detecte_rate_limit_francais():
    cls = get_driver("grok")
    # message reellement affiche par l'UI quand le quota est epuise
    driver = cls(
        TextSession(
            "Limite levée dans 12 heures 36 minutes\n"
            "Atteignez ou passez à SuperGrok pour bénéficier de limites "
            "beaucoup plus élevées"
        )
    )
    assert driver.is_rate_limited() is True
    # un message de test contenant juste « limite » ne doit pas declencher
    driver2 = cls(TextSession("... limite quand x tend vers 0 de sin(x)/x ..."))
    assert driver2.is_rate_limited() is False
