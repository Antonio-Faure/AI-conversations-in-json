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
    def test_daily(self):
        args = run_cli.build_arg_parser().parse_args(["--daily"])
        assert args.daily and not args.monthly

    def test_monthly(self):
        args = run_cli.build_arg_parser().parse_args(["--monthly"])
        assert args.monthly and not args.daily

    def test_full_alias(self):
        args = run_cli.build_arg_parser().parse_args(["--full"])
        assert args.monthly and not args.daily

    def test_modes_exclusifs(self):
        with pytest.raises(SystemExit):
            run_cli.build_arg_parser().parse_args(["--monthly", "--daily"])

    def test_options(self):
        args = run_cli.build_arg_parser().parse_args(
            ["--daily", "--limit", "5", "--headful", "--verbose"]
        )
        assert (args.limit, args.headful, args.verbose) == (5, True, True)

    def test_output_option(self):
        args = run_cli.build_arg_parser().parse_args(["--daily", "--output", "/tmp/x"])
        assert str(args.output) == "/tmp/x"

    def test_screenshots_option(self):
        args = run_cli.build_arg_parser().parse_args(["--daily", "--screenshots"])
        assert args.screenshots is True

    def test_match_option(self):
        args = run_cli.build_arg_parser().parse_args(
            ["--daily", "--match", "Conversation etalon"]
        )
        assert args.match == "Conversation etalon"

    def test_service_repetable(self):
        args = run_cli.build_arg_parser().parse_args(
            ["--daily", "--service", "chatgpt", "-s", "claude"]
        )
        assert args.service == ["chatgpt", "claude"]

    def test_service_inconnu_rejete(self):
        with pytest.raises(SystemExit):
            run_cli.build_arg_parser().parse_args(["--daily", "--service", "copilot"])

    def test_login_choix_service(self):
        for service in ("claude", "grok", "mistral"):
            args = run_cli.build_arg_parser().parse_args(["--login", service])
            assert args.login == service
        with pytest.raises(SystemExit):
            run_cli.build_arg_parser().parse_args(["--login", "copilot"])

    def test_login_cookie_service_capture(self, monkeypatch):
        calls = {}

        def fake_capture(service, cookies_dir, config):
            calls["service"] = service
            calls["dir"] = str(cookies_dir)
            return 0

        monkeypatch.setattr(run_cli, "capture_cookies", fake_capture)
        assert run_cli.main(["--login", "grok"]) == 0
        assert calls["service"] == "grok"
        assert calls["dir"].endswith("cookies")


class TestMain:
    def test_sans_mode_erreur(self):
        assert run_cli.main([]) == 2

    def test_main_appelle_orchestrateur(self, tmp_path, monkeypatch, capsys):
        calls = {}

        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                calls["config"] = config

            def run(self, mode, limit=None, services=None, parallel=None, match=None):
                calls["run"] = {"mode": mode, "limit": limit, "services": services}
                summary = RunSummary(mode=mode)
                summary.services["chatgpt"] = ServiceResult(
                    service="chatgpt", discovered=3, targets=2,
                    exported=[tmp_path / "x.json"],
                )
                return summary

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        code = run_cli.main(["--daily", "--service", "chatgpt"])
        assert code == 0
        assert calls["run"]["mode"] == "daily"
        assert calls["run"]["services"] == ["chatgpt"]
        assert calls["config"]["headless"] is True  # config.yaml du depot
        out = capsys.readouterr().out
        assert "chatgpt" in out and "ecrites=1" in out

    def test_match_transmis(self, monkeypatch):
        calls = {}

        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                pass

            def run(self, mode, limit=None, services=None, parallel=None, match=None):
                calls["match"] = match
                return RunSummary(mode=mode)

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        assert run_cli.main(["--daily", "--match", "Conversation etalon"]) == 0
        assert calls["match"] == "Conversation etalon"

    def test_screenshots_active_config(self, monkeypatch):
        calls = {}

        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                calls["config"] = config

            def run(self, mode, limit=None, services=None, parallel=None, match=None):
                return RunSummary(mode=mode)

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        assert run_cli.main(["--daily", "--screenshots"]) == 0
        assert calls["config"]["screenshots"] is True

    def test_monthly(self, monkeypatch):
        calls = {}

        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                pass

            def run(self, mode, limit=None, services=None, parallel=None, match=None):
                calls["mode"] = mode
                return RunSummary(mode=mode)

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        assert run_cli.main(["--full"]) == 0
        assert calls["mode"] == "monthly"

    def test_main_exit_1_si_failures(self, monkeypatch):
        class StubOrchestrator:
            def __init__(self, config, **kwargs):
                pass

            def run(self, mode, limit=None, services=None, parallel=None, match=None):
                summary = RunSummary(mode=mode)
                summary.services["chatgpt"] = ServiceResult(
                    service="chatgpt", failed=["c1"]
                )
                return summary

        monkeypatch.setattr(run_cli, "Orchestrator", StubOrchestrator)
        assert run_cli.main(["--daily"]) == 1

    def test_chemins_resolus_relatifs_au_depot(self):
        config = {"output_dir": "exports", "profile_dir": "profiles"}
        out = run_cli._resolve_paths(dict(config))
        assert out["output_dir"] == str(ROOT / "exports")
        assert out["profile_dir"] == str(ROOT / "profiles")


class TestLoginSession:
    """Le login utilise le meme moteur que l'export (cookies lies a l'UA)."""

    def test_chatgpt_login_en_playwright(self):
        from src.browser import BrowserSession

        config = run_cli._resolve_paths(run_cli.load_config())
        session = run_cli._login_session("chatgpt", config)
        assert isinstance(session, BrowserSession)
        assert not session.headless

    def test_login_utilise_engine_botasaurus(self):
        from src.browser_botasaurus import BotasaurusSession

        config = run_cli._resolve_paths(run_cli.load_config())
        config["services"]["chatgpt"]["engine"] = "botasaurus"
        session = run_cli._login_session("chatgpt", config)
        assert isinstance(session, BotasaurusSession)
        assert not session.headless
