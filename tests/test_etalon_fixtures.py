"""Tests offline des etalons geles (regression de parseur sans navigateur).

Pour chaque `tests/fixtures/etalons/<platform>/<label>.html`, on reparse avec le
parseur de la plateforme et on compare au JSON gele (nombre de messages, roles,
capacites detectables). Les fixtures sont produites par `scripts/freeze_etalon.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from src.parsers.chatgpt import ChatGPTParser
from src.parsers.claude import ClaudeParser
from src.parsers.gemini import GeminiParser
from src.parsers.mistral import MistralParser
from src.parsers.perplexity import PerplexityParser
from src.utils.etalon import DETECTABLE, detect_capabilities

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "etalons"

PARSERS = {
    "chatgpt": ChatGPTParser,
    "claude": ClaudeParser,
    "gemini": GeminiParser,
    "mistral": MistralParser,
    "perplexity": PerplexityParser,
}


def _cases() -> Iterator[Tuple[str, Path, Path]]:
    if not FIXTURES.exists():
        return
    for platform_dir in sorted(FIXTURES.glob("*")):
        platform = platform_dir.name
        if platform not in PARSERS:
            continue
        for meta in sorted(platform_dir.glob("*.json")):
            html = meta.with_suffix(".html")
            if html.exists():
                yield platform, meta, html


CASES = list(_cases())


@pytest.mark.parametrize("platform,meta,html", CASES, ids=[c[1].stem for c in CASES])
def test_reparse_fixture(platform: str, meta: Path, html: Path):
    expected = json.loads(meta.read_text(encoding="utf-8"))
    parser = PARSERS[platform]()
    conv = parser.parse(
        html.read_text(encoding="utf-8"),
        conversation_id=expected.get("conversation_id") or meta.stem,
        extra={},
    )
    exp_messages = expected.get("messages") or []
    assert len(conv.messages) == len(exp_messages), (
        f"{meta.name}: {len(conv.messages)} messages reparse vs {len(exp_messages)} geles"
    )
    assert [m.role for m in conv.messages] == [m["role"] for m in exp_messages]

    expected_caps = set(expected.get("detected_capabilities") or []) & DETECTABLE
    if expected_caps:
        detected = detect_capabilities(conv.to_dict(validate=False))
        assert expected_caps <= detected, f"{meta.name}: {sorted(expected_caps - detected)}"
