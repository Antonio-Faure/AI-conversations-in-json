"""Tests du scrape a la demande (src/utils/scrape_job.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.scrape_job import DEFAULT_MODE, ScrapeJob, run_platform_scrape


class _FakeResult:
    """ServiceResult minimal (compteurs du run)."""

    def __init__(self, **overrides):
        self.service = "grok"
        self.discovered = 10
        self.targets = 3
        self.exported = [Path("a.json"), Path("b.json")]
        self.patched = ["c"]
        self.failed = []
        self.skipped_reason = None
        for key, value in overrides.items():
            setattr(self, key, value)

    @property
    def ok(self):
        return not self.failed and self.skipped_reason is None


class _FakeOrchestrator:
    def __init__(self, result=None, error=None):
        self.calls = []
        self._result = result or _FakeResult()
        self._error = error

    def run_service(self, name, mode, limit=None, match=None, retry_unavailable=False):
        self.calls.append((name, mode, limit))
        if self._error:
            raise self._error
        return self._result


def test_run_platform_scrape_bilan():
    orchestrator = _FakeOrchestrator()
    bilan = run_platform_scrape(
        {"output_dir": "exports"}, "grok", "daily",
        orchestrator=orchestrator, log=lambda *_: None,
    )
    assert bilan == {
        "platform": "grok", "mode": "daily", "discovered": 10, "targets": 3,
        "exported": 2, "patched": 1, "failed": 0, "skipped": None, "ok": True,
    }
    assert orchestrator.calls == [("grok", "daily", None)]


def test_run_platform_scrape_avec_limite():
    orchestrator = _FakeOrchestrator()
    run_platform_scrape({}, "grok", "monthly", limit=5,
                        orchestrator=orchestrator, log=lambda *_: None)
    assert orchestrator.calls == [("grok", "monthly", 5)]


def test_run_platform_scrape_mode_inconnu():
    with pytest.raises(ValueError):
        run_platform_scrape({}, "grok", "hebdo",
                            orchestrator=_FakeOrchestrator(), log=lambda *_: None)


def test_run_platform_scrape_plateforme_ignoree():
    orchestrator = _FakeOrchestrator(_FakeResult(skipped_reason="disabled"))
    bilan = run_platform_scrape({}, "grok", orchestrator=orchestrator, log=lambda *_: None)
    assert bilan["skipped"] == "disabled"
    assert bilan["ok"] is False


def test_scrape_job_tache_de_fond(tmp_path):
    seen = []

    def runner(config, platform, mode=DEFAULT_MODE, limit=None,
               orchestrator=None, log=print):
        seen.append((platform, mode, limit))
        return {
            "platform": platform, "mode": mode, "discovered": 1, "targets": 1,
            "exported": 1, "patched": 0, "failed": 0, "skipped": None, "ok": True,
        }

    job = ScrapeJob(
        runner=runner,
        lock_path=str(tmp_path / "run.lock"),
        orchestrator_factory=lambda config: object(),
    )
    state = job.start({}, ["grok", "claude"], mode="daily")
    assert state["mode"] == "daily"
    assert state["platforms"] == ["grok", "claude"]
    job._thread.join(timeout=10)
    snapshot = job.snapshot()
    assert snapshot["running"] is False
    assert snapshot["finished_at"]
    assert [item["platform"] for item in snapshot["done"]] == ["grok", "claude"]
    assert seen == [("grok", "daily", None), ("claude", "daily", None)]


def test_scrape_job_un_seul_a_la_fois(tmp_path):
    import threading

    release = threading.Event()

    def blocking(*args, **kwargs):
        release.wait(timeout=5)
        return None

    job = ScrapeJob(
        runner=blocking,
        lock_path=str(tmp_path / "run.lock"),
        orchestrator_factory=lambda config: object(),
    )
    job.start({}, ["grok"])
    with pytest.raises(RuntimeError):
        job.start({}, ["grok"])
    release.set()
    job._thread.join(timeout=5)
