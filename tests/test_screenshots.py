"""Tests des screenshots par message (src/utils/screenshots.py + BaseService)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.services.base import BaseService
from src.utils.screenshots import (
    MAX_TURNS,
    screenshot_reset_body,
    screenshot_step_body,
)

RESET_MARKER = "return !!first;"


class FakeSession:
    """Simule un fil virtualise : une fenetre de `window` tours est montee."""

    def __init__(
        self,
        turns: int,
        window: int | None = None,
        step: int | None = None,
        identical: bool = False,
    ):
        self.turns = turns
        self.window = window or max(turns, 1)
        self.step_size = step or self.window
        self.identical = identical
        self.pos = 0
        self.seen = []
        self.captured = []
        self.steps = []

    def eval_body(self, body):
        if RESET_MARKER in body:
            self.pos = 0
            self.seen = []
            return self.turns > 0
        mounted = range(self.pos, min(self.turns, self.pos + self.window))
        for i in mounted:
            if i not in self.seen:
                self.seen.append(i)
                self.steps.append("shot")
                return "shot"
        self.steps.append("advance")
        self.pos = min(self.turns, self.pos + self.step_size)
        return "advance"

    def wait_ms(self, ms):
        pass

    def screenshot(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = b"png" if self.identical else b"png-%s" % target.name.encode()
        target.write_bytes(data)
        self.captured.append(target.name)
        return target


def _service(session, selectors=("div.msg",)):
    return SimpleNamespace(parser=SimpleNamespace(message_selectors=selectors), session=session)


def test_bodies_injectent_selecteurs():
    for body in (
        screenshot_reset_body(["div.msg", "[data-x]"]),
        screenshot_step_body(["div.msg", "[data-x]"]),
    ):
        assert '"div.msg"' in body and '"[data-x]"' in body
    assert "scrollIntoView" in screenshot_step_body(["div.msg"])
    assert "window.__aicvShots = {seen: []}" in screenshot_reset_body(["div.msg"])


def test_capture_un_screenshot_par_tour(tmp_path):
    session = FakeSession(turns=3, window=3)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == 3
    assert (tmp_path / "message-01.png").exists()
    assert (tmp_path / "message-03.png").exists()
    assert not (tmp_path / "message-04.png").exists()


def test_capture_fil_virtualise_complet(tmp_path):
    session = FakeSession(turns=50, window=8)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == 50
    assert (tmp_path / "message-50.png").exists()
    assert not (tmp_path / "message-51.png").exists()
    assert not list(tmp_path.glob(".aicv-tmp-*"))


def test_capture_dedup_fenetres_chevauchees(tmp_path):
    session = FakeSession(turns=30, window=10, step=6)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == 30
    assert len(session.captured) == len(set(session.captured)) == 30


def test_capture_sans_selecteurs(tmp_path):
    session = FakeSession(turns=3)
    assert BaseService.capture_message_screenshots(_service(session, ()), None, tmp_path) == 0
    assert session.captured == []


def test_capture_sans_support(tmp_path):
    service = SimpleNamespace(parser=SimpleNamespace(message_selectors=("div.msg",)), session=object())
    assert BaseService.capture_message_screenshots(service, None, tmp_path) == 0


def test_images_identiques_dedupliquees(tmp_path):
    session = FakeSession(turns=4, window=4, identical=True)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == 1
    assert (tmp_path / "message-01.png").exists()
    assert not (tmp_path / "message-02.png").exists()
    assert not list(tmp_path.glob(".aicv-tmp-*"))


def test_max_turns_garde_fou(tmp_path):
    session = FakeSession(turns=MAX_TURNS + 50)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == MAX_TURNS
