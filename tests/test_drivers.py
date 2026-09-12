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


def test_registry_drivers():
    assert get_driver("gemini").name == "gemini"
    assert get_driver("grok").name == "grok"
    with pytest.raises(KeyError):
        get_driver("inconnu")


def test_selecteurs_non_vides():
    for name in ("gemini", "grok"):
        cls = get_driver(name)
        assert cls.input_selectors and cls.send_selectors and cls.file_input_selectors
