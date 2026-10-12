"""Tests du ShapeMonitor (accumulateur) et de l'integration orchestrateur."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.orchestrator import DEFAULT_CONFIG, Orchestrator, RunSummary, ServiceResult, deep_merge
from src.schema import Conversation, ConversationRef, Message
from src.services.base import ApiShapeBreakError, BaseService, ShapeMonitor
from src.utils.api_shape import shape_of, signature_of

ROOT = Path(__file__).resolve().parent.parent


def _load_run_cli():
    spec = importlib.util.spec_from_file_location("run_cli_monitor", ROOT / "run.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault("run_cli_monitor", module)
    spec.loader.exec_module(module)
    return module


PAYLOAD = {
    "conversation_id": "c01",
    "title": "Titre",
    "messages": [{"role": "user", "text": "bonjour"}],
}
DRIFTED = {
    "conversation_id": "c01",
    "title": "Titre",
    "extra": {"nouveau": 1},
    "messages": [{"role": "user", "text": "bonjour"}],
}


def write_reference(directory: Path, platform: str, payload) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    doc = {
        "platform": platform,
        "signature": signature_of(payload),
        "shape": shape_of(payload),
    }
    (directory / f"{platform}.shape.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )


class TestShapeMonitor:
    def _monitor(self, payload=PAYLOAD, **kwargs) -> ShapeMonitor:
        return ShapeMonitor(
            platform="test",
            reference={"shape": shape_of(payload), "signature": signature_of(payload)},
            **kwargs,
        )

    def test_accumulation(self):
        monitor = self._monitor()
        report = monitor.record(PAYLOAD)
        assert report["score"] == 100.0 and report["level"] == "OK"
        drifted = monitor.record(DRIFTED)
        assert monitor.n == 2
        assert monitor.matches == 1
        assert monitor.drifts == 1
        assert monitor.mean_score < 100
        assert monitor.min_score == drifted["score"]
        assert "$.extra" in monitor.drift_keys

    def test_resume(self):
        monitor = self._monitor()
        monitor.record(PAYLOAD)
        summary = monitor.summary()
        assert summary["n"] == 1
        assert summary["mean_score"] == 100.0
        assert summary["min_score"] == 100.0
        assert summary["drifts"] == 0
        doc = monitor.to_dict()
        assert doc["platform"] == "test"
        assert doc["updated_at"].endswith("Z")
        assert doc["signature"] == signature_of(PAYLOAD)

    def test_desactive(self):
        monitor = self._monitor(enabled=False)
        assert monitor.record(PAYLOAD) is None
        assert monitor.n == 0

    def test_sans_reference(self):
        monitor = ShapeMonitor(platform="test")
        assert monitor.record(PAYLOAD) is None

    def test_seuil_configurable(self):
        monitor = self._monitor(threshold=50.0)
        monitor.record(DRIFTED)
        assert monitor.drifts == 0 and monitor.matches == 1

    def test_break_compte(self):
        monitor = self._monitor(payload={"a": {"b": 1}})
        report = monitor.record({"autre": [1, 2, 3]})
        assert report["level"] == "BREAK"
        assert monitor.breaks == 1
        assert monitor.drifts == 1

    def test_for_service_depuis_config(self, tmp_path, monkeypatch):
        write_reference(tmp_path, "chatgpt", PAYLOAD)
        monkeypatch.setattr("src.services.base.API_SHAPES_DIR", tmp_path)
        config = {"services": {"chatgpt": {"api_health": {"threshold": 90}}}}
        monitor = ShapeMonitor.for_service("chatgpt", config)
        assert monitor.enabled and monitor.threshold == 90.0
        assert monitor.on_break == "warn"
        assert monitor.reference is not None
        monitor.record(PAYLOAD)
        assert monitor.n == 1

    def test_for_service_reference_absente(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.services.base.API_SHAPES_DIR", tmp_path)
        monitor = ShapeMonitor.for_service("inconnu", {})
        assert monitor.reference is None
        assert monitor.record(PAYLOAD) is None


class _MonitorService(BaseService):
    """Service API factice : enregistre la forme du payload a chaque export."""

    name = "shapefake"
    payload = PAYLOAD

    def build_parser(self):
        return None

    def list_conversations(self, limit=None):
        return [
            ConversationRef(
                service=self.name, id=f"c{i:02d}",
                url=f"https://shape.test/{i}", title=f"Conversation {i}",
            )
            for i in range(1, 4)
        ]

    def export_conversation_with_html(self, ref):
        self.record_api_shape(dict(self.payload))
        conv = Conversation(
            platform=self.name,
            conversation_id=ref.id,
            title=ref.title,
            messages=[Message("user", "bonjour")],
        )
        return conv, None


class _FakeSession:
    def __init__(self, profile_dir, service, config):
        self.closed = False

    def wait_ms(self, ms):
        pass

    def close(self):
        self.closed = True


def make_orchestrator(tmp_path, service_cls, health_cfg=None):
    config = deep_merge(
        DEFAULT_CONFIG,
        {
            "services": {
                "shapefake": {"enabled": True, "api_health": health_cfg or {}}
            }
        },
    )
    config["output_dir"] = str(tmp_path / "exports")
    config["profile_dir"] = str(tmp_path / "profiles")
    config["screenshot_dir"] = ""

    def factory(profile_dir, service, cfg):
        return _FakeSession(profile_dir, service, cfg)

    return Orchestrator(
        config, registry={"shapefake": service_cls}, browser_factory=factory
    )


class TestOrchestratorApiHealth:
    @pytest.fixture(autouse=True)
    def _reference(self, tmp_path, monkeypatch):
        write_reference(tmp_path / "shapes", "shapefake", PAYLOAD)
        monkeypatch.setattr("src.services.base.API_SHAPES_DIR", tmp_path / "shapes")
        _MonitorService.payload = PAYLOAD

    def test_ecrit_api_health_et_resume(self, tmp_path):
        orch = make_orchestrator(tmp_path, _MonitorService)
        result = orch.run(mode="monthly").services["shapefake"]
        assert len(result.exported) == 3
        assert result.api_health is not None
        assert result.api_health["n"] == 3
        assert result.api_health["mean_score"] == 100.0
        assert result.api_health["drifts"] == 0
        health = json.loads(
            (tmp_path / "exports" / "shapefake" / "api_health.json").read_text(
                encoding="utf-8"
            )
        )
        assert health["platform"] == "shapefake"
        assert health["n"] == 3
        assert health["signature"] == signature_of(PAYLOAD)

    def test_drift_accumule_les_cles(self, tmp_path):
        _MonitorService.payload = DRIFTED
        orch = make_orchestrator(tmp_path, _MonitorService)
        result = orch.run(mode="monthly").services["shapefake"]
        assert result.api_health["drifts"] == 3
        assert "$.extra" in result.api_health["drift_keys"]

    def test_desactive_aucun_fichier(self, tmp_path):
        orch = make_orchestrator(tmp_path, _MonitorService, {"enabled": False})
        result = orch.run(mode="monthly").services["shapefake"]
        assert result.api_health is None
        assert not (tmp_path / "exports" / "shapefake" / "api_health.json").exists()

    def test_reference_absente_aucun_fichier(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "src.services.base.API_SHAPES_DIR", tmp_path / "vide"
        )
        orch = make_orchestrator(tmp_path, _MonitorService)
        result = orch.run(mode="monthly").services["shapefake"]
        assert result.api_health is None
        assert not (tmp_path / "exports" / "shapefake" / "api_health.json").exists()

    def test_on_break_stop_arrete_le_service(self, tmp_path):
        class BrokenService(_MonitorService):
            payload = {"autre": [1, 2, 3]}

        orch = make_orchestrator(tmp_path, BrokenService, {"on_break": "stop"})
        result = orch.run(mode="monthly").services["shapefake"]
        assert result.targets == 3
        assert result.exported == []
        assert len(result.skipped) == 1
        assert "rupture de forme" in (result.skipped_reason or "")
        # le bilan est quand meme ecrit (1 payload score)
        health = json.loads(
            (tmp_path / "exports" / "shapefake" / "api_health.json").read_text(
                encoding="utf-8"
            )
        )
        assert health["n"] == 1
        assert health["breaks"] == 1

    def test_on_break_warn_par_defaut(self, tmp_path):
        class BrokenService(_MonitorService):
            payload = {"autre": [1, 2, 3]}

        orch = make_orchestrator(tmp_path, BrokenService)
        result = orch.run(mode="monthly").services["shapefake"]
        assert len(result.exported) == 3  # le run continue
        assert result.api_health["breaks"] == 3


class TestRecordApiShape:
    def test_break_leve_si_stop(self, tmp_path, monkeypatch):
        write_reference(tmp_path, "mon", {"a": 1})
        monkeypatch.setattr("src.services.base.API_SHAPES_DIR", tmp_path)
        service = _MonitorService(_FakeSession(None, "mon", {}), {})
        service.name = "mon"
        service.shape_monitor = ShapeMonitor.for_service(
            "mon", {"services": {"mon": {"api_health": {"on_break": "stop"}}}}
        )
        with pytest.raises(ApiShapeBreakError):
            service.record_api_shape({"z": [1, 2, 3]})

    def test_warn_ne_leve_pas(self, tmp_path, monkeypatch):
        write_reference(tmp_path, "mon", {"a": 1, "b": 2})
        monkeypatch.setattr("src.services.base.API_SHAPES_DIR", tmp_path)
        service = _MonitorService(_FakeSession(None, "mon", {}), {})
        service.name = "mon"
        report = service.record_api_shape({"a": 1, "b": 2, "c": 3})
        assert report["level"] != "OK"
        assert service.shape_monitor.n == 1


class TestSummaryLine:
    def test_api_health_dans_le_resume(self, capsys):
        run_cli = _load_run_cli()
        summary = RunSummary(mode="daily")
        summary.services["chatgpt"] = ServiceResult(
            service="chatgpt",
            api_health={"mean_score": 98.7, "n": 42, "drifts": 2},
        )
        run_cli._print_summary(summary, {"output_dir": "/tmp/x"})
        out = capsys.readouterr().out
        assert "api_health: 98.7% n=42 drift=2" in out

    def test_sans_health_pas_de_ligne(self, capsys):
        run_cli = _load_run_cli()
        summary = RunSummary(mode="daily")
        summary.services["chatgpt"] = ServiceResult(service="chatgpt")
        run_cli._print_summary(summary, {"output_dir": "/tmp/x"})
        assert "api_health" not in capsys.readouterr().out
