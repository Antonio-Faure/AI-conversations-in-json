"""Tests offline des etalons geles (regression de parseur sans navigateur).

Pour chaque `tests/fixtures/etalons/<platform>/<label>.json`, on reparse
l'artefact brut gele et on compare au JSON gele (nombre de messages, roles,
capacites detectables) :

- plateformes API (chatgpt, claude, gemini, grok, mistral, perplexity) : payload
  brut `<label>.raw.json` via `extra={"api": payload}` (Grok : conversation +
  responses ; Gemini : + `title_hint`).

Les fixtures sont produites par `scripts/freeze_etalon.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, Tuple

import pytest

from src.parsers.chatgpt import ChatGPTParser
from src.parsers.claude import ClaudeParser
from src.parsers.gemini import GeminiParser
from src.parsers.grok import GrokParser
from src.parsers.mistral import MistralParser
from src.parsers.perplexity import PerplexityParser
from src.utils.etalon import DETECTABLE, detect_capabilities

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "etalons"

PARSERS = {
    "chatgpt": ChatGPTParser,
    "claude": ClaudeParser,
    "gemini": GeminiParser,
    "grok": GrokParser,
    "mistral": MistralParser,
    "perplexity": PerplexityParser,
}

#: plateformes API : l'artefact gele est le payload brut (`<label>.raw.json`)
API_PLATFORMS = ("chatgpt", "claude", "gemini", "grok", "mistral", "perplexity")


def _raw_fixture(meta: Path) -> Path:
    return meta.parent / f"{meta.stem}.raw.json"


def _parse_fixture(platform: str, meta: Path, artifact: Path):
    """Reparse l'artefact gele (payload API ou HTML) avec le parser courant."""
    expected = json.loads(meta.read_text(encoding="utf-8"))
    conv_id = expected.get("conversation_id") or meta.stem
    parser = PARSERS[platform]()
    if platform in API_PLATFORMS:
        payload: Dict[str, Any] = json.loads(artifact.read_text(encoding="utf-8"))
        if platform == "grok":
            extra: Dict[str, Any] = {
                "conversation": payload.get("conversation") or {},
                "responses": payload.get("responses") or [],
            }
        elif platform == "gemini":
            extra = {"api": payload, "title_hint": expected.get("title")}
        else:
            extra = {"api": payload}
        return parser.parse("", conversation_id=conv_id, extra=extra)
    return parser.parse(
        artifact.read_text(encoding="utf-8"), conversation_id=conv_id, extra={}
    )


def _cases() -> Iterator[Tuple[str, Path, Path]]:
    if not FIXTURES.exists():
        return
    for platform_dir in sorted(FIXTURES.glob("*")):
        platform = platform_dir.name
        if platform not in PARSERS:
            continue
        for meta in sorted(platform_dir.glob("*.json")):
            if meta.name.endswith(".raw.json"):
                continue
            artifact = (
                _raw_fixture(meta) if platform in API_PLATFORMS
                else meta.with_suffix(".html")
            )
            if artifact.exists():
                yield platform, meta, artifact


CASES = list(_cases())


@pytest.mark.parametrize("platform,meta,artifact", CASES, ids=[c[1].stem for c in CASES])
def test_reparse_fixture(platform: str, meta: Path, artifact: Path):
    expected = json.loads(meta.read_text(encoding="utf-8"))
    conv = _parse_fixture(platform, meta, artifact)
    exp_messages = expected.get("messages") or []
    assert len(conv.messages) == len(exp_messages), (
        f"{meta.name}: {len(conv.messages)} messages reparse vs {len(exp_messages)} geles"
    )
    assert [m.role for m in conv.messages] == [m["role"] for m in exp_messages]

    expected_caps = set(expected.get("detected_capabilities") or []) & DETECTABLE
    if expected_caps:
        html = (
            artifact.read_text(encoding="utf-8")
            if platform not in API_PLATFORMS
            else ""
        )
        detected = detect_capabilities(conv.to_dict(validate=False), html)
        assert expected_caps <= detected, f"{meta.name}: {sorted(expected_caps - detected)}"
