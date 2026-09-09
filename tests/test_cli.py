"""Tests du CLI run.py (parsing des arguments + main avec orchestrateur stubbe)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_cli", ROOT / "run.py")
run_cli = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("run_cli", run_cli)
spec.loader.exec_module(run_cli)

from src.orchestrator import RunSummary, ServiceResult  # noqa: E402


class TestArgParser:
    def test_service_repetable(self):
        args = run_cli.build_arg_parser().parse_args(["--service", "chatgpt", "-s", "claude"])
        assert args.service == ["chatgpt", "claude"]
        assert not args.all

    def test_all(self):
        args = run_cli.build_arg_parser().parse_args(["--all"])
        assert args.all

    def test_service_et_all_exclusifs(self):
        with pytest.raises(SystemExit):
            run_cli.build_arg_parser().parse_args(["--all", "--service", "gemini"])

    def test_service_inconnu_rejete(self):
        with pytest.raises(SystemExit):
            run_cli.build_arg_parser().parse_args(["--service", "copilot"])

    def test_options(self):
        args = run_cli.build_arg_parser().parse_args(
            ["--all", "--date", "2026-01-02", "--limit", "5", "--force", "--headful"]
        )
        assert (args.date, args.limit, args.force, args.headful) == ("2026-01-02", 5, True, True)


class TestMain:
    def test_sans_cibleErreur(self, capsys):
        assert run_cli.main([]) == 2

    def test_date_invalide(self):
        assert run_cli.main(["--all", "--date", "2026-13-99"]) == 2

    def test_main_appelle_orchestrateur(self, tmp_path, monkeypatch, capsys):
        calls = {}

        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                calls["config"] = config

            def run(self, **kwargs):
                calls["run"] = kwargs
                summary = RunSummary(date="2026-05-01")
                summary.services["chatgpt"] = ServiceResult(
                    service="chatgpt", exported=[tmp_path / "x.json"]
                )
                return summary

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        code = run_cli.main(["--service", "chatgpt", "--date", "2026-05-01"])
        assert code == 0
        assert calls["run"]["services"] == ["chatgpt"]
        assert calls["run"]["date"] == "2026-05-01"
        assert calls["config"]["headless"] is True  # config.yaml du depot
        out = capsys.readouterr().out
        assert "chatgpt" in out and "exportees=1" in out

    def test_main_exit_1_si_failures(self, tmp_path, monkeypatch, capsys):
        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                pass

            def run(self, **kwargs):
                summary = RunSummary(date="2026-05-01")
                summary.services["claude"] = ServiceResult(service="claude", failed=["c1"])
                return summary

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        assert run_cli.main(["--all"]) == 1

    def test_chemins_resolus_relatifs_au_depot(self, monkeypatch):
        config = {"output_dir": "exports", "profile_dir": "profiles",
                  "state_file": ".state/state.json", "log_file": "logs/a.jsonl"}
        out = run_cli._resolve_paths(dict(config))
        assert out["output_dir"] == str(ROOT / "exports")
        assert str(out["state_file"]).endswith(".state/state.json")
