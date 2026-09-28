"""Fixtures pytest partagees : chemins des fixtures HTML, logging discret."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fixture_html():
    def load(name: str) -> str:
        path = FIXTURES_DIR / name
        assert path.exists(), f"fixture manquante: {path}"
        return path.read_text(encoding="utf-8")

    return load


@pytest.fixture(autouse=True)
def _quiet_logging():
    logging.getLogger("aicv").setLevel(logging.CRITICAL)
    yield
    logging.getLogger("aicv").setLevel(logging.NOTSET)
