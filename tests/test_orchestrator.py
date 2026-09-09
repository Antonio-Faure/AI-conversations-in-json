"""Tests de l'orchestrateur sans navigateur: faux services injectes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.orchestrator import (
    DEFAULT_CONFIG,
    Orchestrator,
    RunSummary,
    ServiceResult,
    deep_merge,
    load_config,
    resolve_engine,
)
from src.parsers.base import ParseError
from src.schema import Conversation, ConversationRef, Message
from src.services.base import BlockedError, EmptyConversationError, ServiceNotLoggedIn
from src.utils.file_utils import today_str, validate_date_arg


def make_conversation(service="fake", conv_id="c1", n=2, last="2026-05-01T10:00:00Z"):
    conv = Conversation(
        service=service,
        conversation_id=conv_id,
        title=f"Titre {conv_id}",
        messages=[Message("user", f"q{i}", f"2026-05-0{i + 1}T09:00:00Z") for i in range(n)],
    )
    conv.messages[-1].timestamp = last
    conv.derive_timestamps()
    return conv


class FakeSession:
    """Duck-type BrowserSession : ferme proprement, rien d'autre a faire ici."""

    def __init__(self, profile_dir, service, config):
        self.profile_dir = profile_dir
        self.service = service
        self.config = config
        self.closed = False

    def close(self):
        self.closed = True


class FakeService:
    name = "fake"
    home_url = "https://fake.test/"
    instances: list = []

    def __init__(self, session, config):
        self.session = session
        self.config = config
        self.refs = [
            ConversationRef(service="fake", id=f"c{i}", url=f"https://fake.test/c/i{i}")
            for i in range(1, 4)
        ]
        FakeService.instances.append(self)

    def list_conversations(self, limit=None):
        refs = self.refs
        if limit:
            refs = refs[:limit]
        return refs

    def export_conversation(self, ref):
        return make_conversation(service=self.name, conv_id=ref.id)


class ExplodingDiscovery(FakeService):
    name = "boom"

    def list_conversations(self, limit=None):
        raise RuntimeError("sidebar absente")


class ExplodingConversation(FakeService):
    name = "flaky"

    def export_conversation(self, ref):
        if ref.id == "c2":
            raise ParseError("DOM modifie")
        return make_conversation(service=self.name, conv_id=ref.id)


class AuthLostService(FakeService):
    name = "authlost"

    def list_conversations(self, limit=None):
        raise ServiceNotLoggedIn("session expiree")


class BlockedDiscoveryService(FakeService):
    """Echoue en decouverte au 1er appel, reussit ensuite (simulation bascule)."""

    name = "blockdisc"
    calls = 0

    def list_conversations(self, limit=None):
        BlockedDiscoveryService.calls += 1
        if BlockedDiscoveryService.calls == 1:
            raise BlockedError("challenge cloudflare")
        return super().list_conversations(limit=limit)


class BlockedOnceConversation(FakeService):
    """Echoue sur la 1re conversation au 1er passage, reussit apres bascule."""

    name = "blockconv"
    calls = 0

    def export_conversation(self, ref):
        if ref.id == "c1" and BlockedOnceConversation.calls == 0:
            BlockedOnceConversation.calls += 1
            raise BlockedError("challenge cloudflare")
        return make_conversation(service=self.name, conv_id=ref.id)


class AlwaysBlockedDiscovery(FakeService):
    name = "hardblock"

    def list_conversations(self, limit=None):
        raise BlockedError("challenge cloudflare")


class EmptyConversationService(FakeService):
    """Conversation non chargeable (vide/tache) : skip, pas un echec."""

    name = "emptyconv"

    def export_conversation(self, ref):
        raise EmptyConversationError(f"{self.name}: conversation sans message")


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_orchestrator(tmp_path, registry, config_extra=None):
    config = deep_merge(DEFAULT_CONFIG, config_extra or {})
    config["output_dir"] = str(tmp_path / "exports")
    config["profile_dir"] = str(tmp_path / "profiles")
    config["state_file"] = str(tmp_path / ".state" / "state.json")
    config["log_file"] = ""
    sessions = []

    def factory(profile_dir, service, cfg):
        session = FakeSession(Path(profile_dir), service, cfg)
        sessions.append(session)
        return session

    orch = Orchestrator(config, registry=registry, browser_factory=factory)
    return orch, sessions


class TestConfig:
    def test_deep_merge(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        out = deep_merge(base, {"nested": {"y": 3, "z": 4}})
        assert out == {"a": 1, "nested": {"x": 1, "y": 3, "z": 4}}

    def test_load_config_surcharge_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump({"headless": True, "services": {"chatgpt": {"enabled": False}}}),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        assert config["headless"] is True
        assert config["services"]["chatgpt"]["enabled"] is False
        assert config["services"]["claude"]["enabled"] is True  # default conserve

    def test_load_config_invalide(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("- liste pas un mapping\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(bad)

    def test_validate_date(self):
        assert validate_date_arg("2026-09-09") == "2026-09-09"
        with pytest.raises(ValueError):
            validate_date_arg("09/09/2026")


class TestRunPipeline:
    def test_export_ecrit_fichiers_standard(self, workdir):
        tmp = workdir
        orch, sessions = make_orchestrator(tmp, {"fake": FakeService})
        summary = orch.run(services=["fake"], date="2026-05-01", force=True)

        assert summary.date == "2026-05-01"
        result = summary.services["fake"]
        assert len(result.exported) == 3
        assert result.failed == []
        assert summary.total_exported == 3
        # sessions fermees
        assert all(s.closed for s in sessions)
        # chemin exports/<date>/<service>/<id>.json
        path = tmp / "exports" / "2026-05-01" / "fake" / "c1.json"
        assert path.exists(), f"attendu {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["service"] == "fake"
        assert data["conversation_id"] == "c1"
        assert data["messages"][0]["role"] == "user"
        assert data["exported_at"]

    def test_date_explicite_filtre_sur_cette_date(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        summary = orch.run(services=["fake"], date="2026-05-03")
        result = summary.services["fake"]
        assert result.exported == []
        assert len(result.out_of_range) == 3

    def test_sans_date_range_par_date_conversation(self, workdir):
        tmp = workdir
        # sans --date: chaque conversation part dans le dossier de SA date
        orch, _ = make_orchestrator(tmp, {"fake": FakeService})
        summary = orch.run(services=["fake"])
        assert len(summary.services["fake"].exported) == 3
        for path in summary.services["fake"].exported:
            assert path.parent.parent == tmp / "exports" / "2026-05-01"
        assert (tmp / "exports" / today_str()).exists() is False

    def test_filter_date_exclut_hors_periode(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        summary = orch.run(services=["fake"], date="2026-05-03", filter_date=True)
        result = summary.services["fake"]
        assert result.exported == []
        assert len(result.out_of_range) == 3

    def test_incremental_unchanged_au_second_run(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        orch.run(services=["fake"])
        orch2, _ = make_orchestrator(workdir, {"fake": FakeService})
        summary = orch2.run(services=["fake"])
        result = summary.services["fake"]
        assert result.exported == []
        assert len(result.unchanged) == 3

        # force re-exporte tout
        summary = orch2.run(services=["fake"], force=True)
        assert len(summary.services["fake"].exported) == 3

    def test_limit(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        summary = orch.run(services=["fake"], limit=2, force=True)
        assert len(summary.services["fake"].exported) == 2

    def test_conversation_en_echec_n_arrete_pas_le_service(self, workdir):
        orch, _ = make_orchestrator(workdir, {"flaky": ExplodingConversation})
        summary = orch.run(services=["flaky"])
        result = summary.services["flaky"]
        assert len(result.exported) == 2
        assert result.failed == ["c2"]
        assert summary.has_failures

    def test_discovery_en_echec(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"boom": ExplodingDiscovery})
        summary = orch.run(services=["boom"])
        result = summary.services["boom"]
        assert result.failed
        assert any("sidebar" in e for e in result.failed)
        assert sessions[0].closed

    def test_auth_requise_skip_service(self, workdir):
        orch, _ = make_orchestrator(workdir, {"authlost": AuthLostService})
        summary = orch.run(services=["authlost"])
        result = summary.services["authlost"]
        assert result.skipped
        assert "session expiree" in result.skip_reason
        assert summary.has_failures  # un skip != disabled compte comme souci

    def test_service_inconnu(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        with pytest.raises(ValueError, match="inconnu"):
            orch.run(services=["ghost"])

    def test_services_desactives_sautes_sans_date(self, workdir):
        config_extra = {"services": {"fake": {"enabled": False}}}
        orch, _ = make_orchestrator(workdir, {"fake": FakeService}, config_extra)
        summary = orch.run()  # aucun service demande -> tout le registre, filtres par enabled
        result = summary.services["fake"]
        assert result.skipped and result.skip_reason == "disabled"
        assert not summary.has_failures
        assert summary.total_exported == 0

    def test_disabled_meme_demand_explicitement(self, workdir):
        config_extra = {"services": {"fake": {"enabled": False}}}
        orch, _ = make_orchestrator(workdir, {"fake": FakeService}, config_extra)
        summary = orch.run(services=["fake"])
        result = summary.services["fake"]
        assert result.skipped and result.skip_reason == "disabled"
        assert not summary.has_failures

    def test_config_url_surcharge_home(self, workdir):
        config_extra = {"services": {"fake": {"url": "https://autre.test/"}}}
        orch, _ = make_orchestrator(workdir, {"fake": FakeService}, config_extra)
        orch.run(services=["fake"])
        assert FakeService.instances[-1].home_url == "https://autre.test/"

    def test_all_services_retourne_registry(self, workdir):
        orch, _ = make_orchestrator(
            workdir, {"fake": FakeService, "other": FakeService}
        )
        summary = orch.run(all_services=True)
        assert set(summary.services) == {"fake", "other"}


class TestEngine:
    def test_resolve_engine_par_defaut_auto(self):
        assert resolve_engine("fake", DEFAULT_CONFIG) == "auto"

    def test_resolve_engine_global(self):
        config = deep_merge(DEFAULT_CONFIG, {"engine": "botasaurus"})
        assert resolve_engine("fake", config) == "botasaurus"

    def test_resolve_engine_override_service(self):
        config = deep_merge(
            DEFAULT_CONFIG,
            {"engine": "playwright", "services": {"claude": {"engine": "botasaurus"}}},
        )
        assert resolve_engine("claude", config) == "botasaurus"
        assert resolve_engine("chatgpt", config) == "playwright"

    def test_resolve_engine_invalide_retombe_auto(self):
        config = deep_merge(DEFAULT_CONFIG, {"engine": "nimportequoi"})
        assert resolve_engine("fake", config) == "auto"

    def test_auto_bascule_botasaurus_sur_discovery_bloquee(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"blockdisc": BlockedDiscoveryService})
        summary = orch.run(services=["blockdisc"], force=True)
        result = summary.services["blockdisc"]
        # 2e tentative avec le moteur botasaurus -> decouverte OK
        assert len(result.exported) == 3
        assert result.failed == []
        assert len(sessions) == 2
        assert sessions[0].config.get("_engine") == "playwright"
        assert sessions[1].config.get("_engine") == "botasaurus"
        assert all(s.closed for s in sessions)

    def test_auto_bascule_botasaurus_sur_conversation_bloquee(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"blockconv": BlockedOnceConversation})
        summary = orch.run(services=["blockconv"], force=True)
        result = summary.services["blockconv"]
        # c1 reussit apres bascule, les suivantes aussi
        assert len(result.exported) == 3
        assert result.failed == []
        assert len(sessions) == 2
        assert sessions[1].config.get("_engine") == "botasaurus"

    def test_bloquage_persistant_skip_service(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"hardblock": AlwaysBlockedDiscovery})
        summary = orch.run(services=["hardblock"])
        result = summary.services["hardblock"]
        # 2 sessions maximum (playwright puis botasaurus), puis skip
        assert result.skipped
        assert "challenge" in result.skip_reason
        assert len(sessions) == 2

    def test_engine_botasaurus_direct_sans_fallback(self, workdir):
        config_extra = {"engine": "botasaurus"}
        orch, sessions = make_orchestrator(
            workdir, {"hardblock": AlwaysBlockedDiscovery}, config_extra
        )
        summary = orch.run(services=["hardblock"])
        result = summary.services["hardblock"]
        # moteur impose : aucune bascule, une seule session, directement botasaurus
        assert result.skipped
        assert len(sessions) == 1
        assert sessions[0].config.get("_engine") == "botasaurus"

    def test_conversation_vide_ignoree_pas_un_echec(self, workdir):
        orch, _ = make_orchestrator(workdir, {"emptyconv": EmptyConversationService})
        summary = orch.run(services=["emptyconv"], force=True)
        result = summary.services["emptyconv"]
        assert result.skipped_items == ["c1", "c2", "c3"]
        assert result.failed == []
        assert not summary.has_failures
        assert result.exported == []


class TestRunSummary:
    def test_ok_et_to_dict(self):
        summary = RunSummary(date="2026-01-01")
        summary.services["a"] = ServiceResult(service="a", exported=[Path("/x/a.json")])
        assert summary.total_exported == 1
        assert not summary.has_failures

    def test_service_result_ok(self):
        assert ServiceResult(service="a").ok
        assert not ServiceResult(service="a", failed=["x"]).ok
