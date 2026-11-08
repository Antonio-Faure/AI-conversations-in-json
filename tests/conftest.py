"""Fixtures pytest partagees : chemins des fixtures HTML, logging discret."""

from __future__ import annotations

import json
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


@pytest.fixture()
def fixture_json():
    """Charge une fixture JSON (payload API gele, export d'etalon)."""

    def load(name: str):
        path = FIXTURES_DIR / name
        assert path.exists(), f"fixture manquante: {path}"
        return json.loads(path.read_text(encoding="utf-8"))

    return load


@pytest.fixture(autouse=True)
def _quiet_logging():
    logging.getLogger("aicv").setLevel(logging.CRITICAL)
    yield
    logging.getLogger("aicv").setLevel(logging.NOTSET)


@pytest.fixture(autouse=True)
def _isolated_run_state(tmp_path, monkeypatch):
    """Isole `.state/aicv-run.json` : les tests ne touchent pas un vrai scrape.

    `run.py` publie son etat dans le depot ; sans cette redirection un test du
    CLI ecraserait l'indicateur d'un cron en cours (et inversement).
    """
    from src.utils import runstate

    monkeypatch.setattr(runstate, "RUN_STATE_PATH", tmp_path / "aicv-run.json")
