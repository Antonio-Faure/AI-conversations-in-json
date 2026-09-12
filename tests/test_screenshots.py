"""Tests des screenshots par message (src/utils/screenshots.py + BaseService)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from src.services.base import BaseService
from src.utils.screenshots import MAX_TURNS, scroll_to_index_body


class FakeSession:
    def __init__(self, turns: int):
        self.turns = turns
        self.scrolled = []
        self.captured = []

    def eval_body(self, body):
        match = re.search(r"const index = (\d+);", body)
        index = int(match.group(1)) if match else 0
        self.scrolled.append(index)
        return index < self.turns

    def wait_ms(self, ms):
        pass

    def screenshot(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"png")
        self.captured.append(target.name)
        return target


def _service(session, selectors=("div.msg",)):
    return SimpleNamespace(parser=SimpleNamespace(message_selectors=selectors), session=session)


def test_scroll_body_injecte_selecteurs_et_index():
    body = scroll_to_index_body(["div.msg", "[data-x]"], 4)
    assert '"div.msg"' in body and '"[data-x]"' in body
    assert "const index = 4;" in body
    assert "scrollIntoView" in body


def test_capture_un_screenshot_par_tour(tmp_path):
    session = FakeSession(turns=3)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == 3
    assert (tmp_path / "message-01.png").exists()
    assert (tmp_path / "message-03.png").exists()
    assert not (tmp_path / "message-04.png").exists()
    assert session.scrolled == [0, 1, 2, 3]


def test_capture_sans_selecteurs(tmp_path):
    session = FakeSession(turns=3)
    assert BaseService.capture_message_screenshots(_service(session, ()), None, tmp_path) == 0
    assert session.scrolled == []


def test_capture_sans_support(tmp_path):
    service = SimpleNamespace(parser=SimpleNamespace(message_selectors=("div.msg",)), session=object())
    assert BaseService.capture_message_screenshots(service, None, tmp_path) == 0


def test_max_turns_garde_fou(tmp_path):
    session = FakeSession(turns=MAX_TURNS + 50)
    count = BaseService.capture_message_screenshots(_service(session), None, tmp_path)
    assert count == MAX_TURNS
