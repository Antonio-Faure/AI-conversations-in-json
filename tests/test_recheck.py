"""Tests du controle de parseur (src/utils/recheck.py)."""

from __future__ import annotations

import fcntl
import json
import threading
from types import SimpleNamespace

import pytest

from src.utils.recheck import (
    RecheckJob,
    compare_conversations,
    pick_conversations,
    platform_verdict,
    read_report,
    run_platform_recheck,
    summarize,
)


def _conv(messages, **extra):
    """Conversation minimale (JSON standardise) pour les metriques."""
    return {"conversation_id": "c1", "platform": "grok", "title": "t",
            "messages": messages, **extra}


def _msg(role, texte):
    return {"role": role, "texte": texte}


def test_pick_conversations_filtre_et_echantillonne():
    entries = [
        {"conversation_id": "a", "message_count": 2},
        {"conversation_id": "b", "message_count": 30},
        {"conversation_id": "c", "message_count": 12},
        {"conversation_id": "d", "message_count": 8},
        {"conversation_id": "", "message_count": 40},
    ]
    picked = pick_conversations(entries, count=2, min_messages=8, seed=1)
    assert len(picked) == 2
    assert all(entry["conversation_id"] in {"b", "c", "d"} for entry in picked)
    # reproductible avec la meme graine
    assert picked == pick_conversations(entries, count=2, min_messages=8, seed=1)
    # moins de candidates que demande -> tout ce qui passe le filtre
    assert len(pick_conversations(entries, count=9, min_messages=30, seed=1)) == 1
    assert pick_conversations([], count=3) == []


def test_compare_conversations_identiques():
    archived = _conv([_msg("user", "salut"), _msg("assistant", "bonjour")])
    comparison = compare_conversations(archived, archived)
    assert comparison["status"] == "ok"
    assert comparison["retention"] == 1.0
    assert comparison["lost"] == []
    assert comparison["old_messages"] == comparison["new_messages"] == 2


def test_compare_conversations_degradees():
    archived = _conv([
        _msg("user", "salut"),
        _msg("assistant", "![img](images/a.png)\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"),
        _msg("user", "encore"),
        _msg("assistant", "du texte"),
    ])
    # le re-scrape perd un message, l'image et le tableau
    fresh = _conv([_msg("user", "salut"), _msg("assistant", "du texte")])
    comparison = compare_conversations(archived, fresh)
    assert comparison["status"] == "degrade"
    assert comparison["retention"] == 0.5
    assert any(loss.startswith("messages") for loss in comparison["lost"])
    assert any(loss.startswith("images") for loss in comparison["lost"])
    assert any(loss.startswith("tables") for loss in comparison["lost"])


def test_compare_conversation_vide_est_signalee():
    archived = _conv([_msg("user", "salut")])
    comparison = compare_conversations(archived, _conv([]))
    assert comparison["status"] == "vide"
    assert comparison["retention"] == 0.0


def test_compare_conversation_qui_a_continue():
    """Plus de messages qu'a l'archive : pas une regression."""
    archived = _conv([_msg("user", "salut")])
    fresh = _conv([_msg("user", "salut"), _msg("assistant", "la suite")])
    comparison = compare_conversations(archived, fresh)
    assert comparison["status"] == "ok"
    assert comparison["retention"] == 2.0
    assert comparison["lost"] == []


def test_verdict_plateforme():
    assert platform_verdict([]) == "inconnu"
    assert platform_verdict([{"status": "ok", "retention": 1.0}]) == "ok"
    assert platform_verdict([{"status": "degrade", "retention": 0.9}]) == "attention"
    assert platform_verdict([
        {"status": "ok", "retention": 1.0},
        {"status": "degrade", "retention": 0.4},
    ]) == "critique"


def test_verdict_ignore_les_limites_de_debit():
    """Un rate-limit ne doit pas passer pour une regression de parseur."""
    assert platform_verdict([{"status": "ignore", "retention": None}]) == "inconnu"
    assert platform_verdict([
        {"status": "ignore", "retention": None},
        {"status": "ok", "retention": 1.0},
    ]) == "ok"
    # un echec isole reste une attention
    assert platform_verdict([
        {"status": "ok", "retention": 1.0},
        {"status": "ok", "retention": 1.0},
        {"status": "echec", "retention": None},
    ]) == "attention"
    # la moitie en echec -> critique
    assert platform_verdict([
        {"status": "ok", "retention": 1.0},
        {"status": "vide", "retention": 0.0},
    ]) == "critique"


def test_summarize_agrege():
    conversations = [
        {"status": "ok", "retention": 1.0, "lost": []},
        {"status": "degrade", "retention": 0.8, "lost": ["images: 3 -> 1"]},
        {"status": "ignore", "retention": None, "lost": []},
    ]
    report = summarize("grok", conversations, seed=3, per_platform=3, min_messages=8)
    assert report["platform"] == "grok"
    assert report["verdict"] == "attention"
    assert report["checked"] == 3
    assert report["compared"] == 2
    assert report["ignored"] == 1
    assert report["ok"] == 1
    assert report["degraded"] == 1
    assert report["failed"] == 0
    assert report["retention"] == 0.9
    assert report["seed"] == 3
    assert report["checked_at"].endswith("Z")
    assert len(report["conversations"]) == 3


# -- re-scrape (orchestrateur et service factices) ----------------------------

class _FakeService:
    """Service factice : rend la conversation « re-scrapee » demandee."""

    def __init__(self, messages):
        self.messages = messages

    def export_conversation_with_html(self, ref):
        payload = {"conversation_id": ref.id, "platform": "grok",
                   "messages": self.messages}
        return SimpleNamespace(
            to_dict=lambda validate=False: payload
        ), None

    def take_raw_payload(self):
        return None


class _FakeOrchestrator:
    def __init__(self, service):
        self.registry = {"grok": object}
        self._service = service
        self.closed = False

    def _new_session(self, platform, cls, svc_config, engine):
        return None, self._service

    def _close(self, session):
        self.closed = True


def _seed_platform(tmp_path, messages):
    """Archive d'une conversation grok (2 messages) + son inventaire."""
    platform_dir = tmp_path / "grok"
    platform_dir.mkdir()
    archived = {"conversation_id": "c1", "platform": "grok", "title": "T",
                "messages": messages}
    (platform_dir / "conv1.json").write_text(json.dumps(archived), encoding="utf-8")
    (platform_dir / "conversation_list.json").write_text(
        json.dumps({"conversations": [
            {"conversation_id": "c1", "title": "T", "message_count": len(messages)},
        ]}),
        encoding="utf-8",
    )
    return archived


def test_run_platform_recheck_ecrit_le_rapport(tmp_path):
    archived = _seed_platform(tmp_path, [
        {"role": "user", "texte": "salut"},
        {"role": "assistant", "texte": "bonjour"},
    ])
    orchestrator = _FakeOrchestrator(_FakeService(archived["messages"]))
    report = run_platform_recheck(
        {"output_dir": str(tmp_path)}, "grok",
        per_platform=1, min_messages=1, seed=0,
        orchestrator=orchestrator, log=lambda *_: None,
    )
    assert report["verdict"] == "ok"
    assert report["retention"] == 1.0
    assert report["compared"] == 1
    assert orchestrator.closed is True
    stored = json.loads((tmp_path / "grok" / "recheck.json").read_text(encoding="utf-8"))
    assert stored["platform"] == "grok"
    assert stored["conversations"][0]["status"] == "ok"


def test_run_platform_recheck_detecte_la_regression(tmp_path):
    _seed_platform(tmp_path, [
        {"role": "user", "texte": "salut"},
        {"role": "assistant", "texte": "![a](images/a.png) du texte"},
        {"role": "user", "texte": "encore"},
        {"role": "assistant", "texte": "la suite"},
    ])
    # le re-scrape ne rend plus qu'un message sur quatre, sans l'image
    orchestrator = _FakeOrchestrator(_FakeService([{"role": "user", "texte": "salut"}]))
    report = run_platform_recheck(
        {"output_dir": str(tmp_path)}, "grok",
        per_platform=1, min_messages=1, seed=0,
        orchestrator=orchestrator, log=lambda *_: None,
    )
    assert report["verdict"] == "critique"  # 1/4 messages retrouves
    assert report["degraded"] == 1
    assert read_report(tmp_path, "grok")["issues"][0]["reason"].startswith("messages")


def test_run_platform_recheck_sans_candidat(tmp_path):
    (tmp_path / "grok").mkdir()
    (tmp_path / "grok" / "conversation_list.json").write_text(
        json.dumps({"conversations": [
            {"conversation_id": "c1", "message_count": 1},
        ]}),
        encoding="utf-8",
    )
    assert run_platform_recheck(
        {"output_dir": str(tmp_path)}, "grok", per_platform=1, min_messages=8,
        log=lambda *_: None,
    ) is None


def test_read_report_absent(tmp_path):
    assert read_report(tmp_path, "grok") is None


# -- job en tache de fond -----------------------------------------------------

def _fake_runner(seen):
    def runner(config, platform, per_platform, min_messages, seed,
               orchestrator=None, log=print):
        seen.append(platform)
        return {
            "platform": platform, "verdict": "ok", "retention": 1.0,
            "checked": 1, "compared": 1, "ignored": 0, "ok": 1,
            "degraded": 0, "failed": 0, "checked_at": "2026-01-01T00:00:00Z",
            "conversations": [],
        }
    return runner


def test_recheck_job_execute_en_tache_de_fond(tmp_path):
    seen = []
    job = RecheckJob(
        runner=_fake_runner(seen),
        lock_path=str(tmp_path / "run.lock"),
        orchestrator_factory=lambda config: object(),
    )
    state = job.start({"output_dir": str(tmp_path)}, ["grok", "claude"], 2, 8, 0)
    assert state["platforms"] == ["grok", "claude"]
    job._thread.join(timeout=10)
    snapshot = job.snapshot()
    assert snapshot["running"] is False
    assert snapshot["finished_at"]
    assert seen == ["grok", "claude"]
    assert [item["platform"] for item in snapshot["done"]] == ["grok", "claude"]
    assert snapshot["log"]


def test_recheck_job_un_seul_a_la_fois(tmp_path):
    release = threading.Event()

    def blocking_runner(*args, **kwargs):
        release.wait(timeout=5)
        return None

    job = RecheckJob(
        runner=blocking_runner,
        lock_path=str(tmp_path / "run.lock"),
        orchestrator_factory=lambda config: object(),
    )
    job.start({}, ["grok"], 1, 8, 0)
    with pytest.raises(RuntimeError):
        job.start({}, ["grok"], 1, 8, 0)
    release.set()
    job._thread.join(timeout=5)


def test_recheck_job_refuse_pendant_un_scrape(tmp_path):
    lock_path = tmp_path / "run.lock"
    handle = open(lock_path, "w")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        job = RecheckJob(
            runner=lambda *a, **k: None,
            lock_path=str(lock_path),
            orchestrator_factory=lambda config: object(),
        )
        job.start({}, ["grok"], 1, 8, 0)
        job._thread.join(timeout=5)
        snapshot = job.snapshot()
        assert snapshot["error"] == "scrape en cours"
        assert snapshot["done"] == []
        assert any("verrou occupe" in line for line in snapshot["log"])
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
