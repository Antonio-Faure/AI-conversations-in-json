"""Tests de l'orchestrateur sans navigateur : faux services injectes."""

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
    _fold,
    deep_merge,
    load_config,
    resolve_engine,
)
from src.parsers.base import ParseError
from src.schema import Conversation, ConversationRef, Message
from src.services.base import BlockedError, EmptyConversationError, ServiceNotLoggedIn


def make_conversation(platform="fake", conv_id="c1", n=2, title=None):
    conv = Conversation(
        platform=platform,
        conversation_id=conv_id,
        title=title or f"Titre {conv_id}",
        messages=[
            Message("user", f"q{i}", timestamp=f"2026-05-0{i + 1}T09:00:00Z")
            for i in range(n)
        ],
    )
    conv.derive_timestamps()
    return conv


class FakeSession:
    """Duck-type BrowserSession : ferme proprement, wait_ms inoffensif."""

    def __init__(self, profile_dir, service, config):
        self.profile_dir = profile_dir
        self.service = service
        self.config = config
        self.closed = False

    def wait_ms(self, ms):
        pass

    def close(self):
        self.closed = True


class FakeService:
    name = "fake"
    home_url = "https://fake.test/"
    ref_count = 3
    instances: list = []

    def __init__(self, session, config):
        self.session = session
        self.config = config
        self.refs = [
            ConversationRef(
                service=self.name,
                id=f"c{i:02d}",
                url=f"https://fake.test/c/{i}",
                title=f"Conversation {i}",
            )
            for i in range(1, self.ref_count + 1)
        ]
        FakeService.instances.append(self)

    def list_conversations(self, limit=None):
        refs = list(self.refs)
        if limit:
            refs = refs[:limit]
        return refs

    def export_conversation_with_html(self, ref):
        conv = make_conversation(platform=self.name, conv_id=ref.id, title=ref.title)
        return conv, f"<html><body><div id='{ref.id}'>{ref.id}</div></body></html>"

    def export_conversation(self, ref):
        return self.export_conversation_with_html(ref)[0]


class ScreenshotService(FakeService):
    name = "fake"

    def capture_message_screenshots(self, conv, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "message-01.png").write_bytes(b"x")
        return 1


class ExplodingDiscovery(FakeService):
    name = "boom"

    def list_conversations(self, limit=None):
        raise RuntimeError("sidebar absente")


class ExplodingConversation(FakeService):
    name = "flaky"

    def export_conversation_with_html(self, ref):
        if ref.id == "c02":
            raise ParseError("DOM modifie")
        return super().export_conversation_with_html(ref)


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
    name = "blockconv"
    calls = 0

    def export_conversation_with_html(self, ref):
        if ref.id == "c01" and BlockedOnceConversation.calls == 0:
            BlockedOnceConversation.calls += 1
            raise BlockedError("challenge cloudflare")
        return super().export_conversation_with_html(ref)


class AlwaysBlockedDiscovery(FakeService):
    name = "hardblock"

    def list_conversations(self, limit=None):
        raise BlockedError("challenge cloudflare")


class EmptyConversationService(FakeService):
    name = "emptyconv"

    def export_conversation_with_html(self, ref):
        raise EmptyConversationError(f"{self.name}: conversation sans message")


class ManyConversations(FakeService):
    name = "fake"
    ref_count = 25


def make_orchestrator(tmp_path, registry, config_extra=None):
    config = deep_merge(DEFAULT_CONFIG, config_extra or {})
    config["output_dir"] = str(tmp_path / "exports")
    config["profile_dir"] = str(tmp_path / "profiles")
    config["screenshot_dir"] = ""
    config["services"] = {"fake": {"enabled": True}}
    config["services"].update((config_extra or {}).get("services") or {})
    sessions = []

    def factory(profile_dir, service, cfg):
        session = FakeSession(Path(profile_dir), service, cfg)
        sessions.append(session)
        return session

    orch = Orchestrator(config, registry=registry, browser_factory=factory)
    return orch, sessions


def read_list(tmp_path, platform="fake"):
    path = tmp_path / "exports" / platform / "conversation_list.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_fake_state():
    """Evite les dependances d'ordre (compteurs/listes de classe)."""
    FakeService.instances = []
    BlockedDiscoveryService.calls = 0
    BlockedOnceConversation.calls = 0
    yield


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
        # les services non surcharges gardent leur defaut (tous actifs)
        assert config["services"]["claude"]["enabled"] is True
        assert config["services"]["gemini"]["enabled"] is True
        assert config["services"]["perplexity"]["enabled"] is True

    def test_load_config_invalide(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("- liste pas un mapping\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(bad)


class TestRunPipeline:
    def test_monthly_ecrit_json_html_et_inventaire(self, workdir):
        tmp = workdir
        orch, sessions = make_orchestrator(tmp, {"fake": FakeService})
        summary = orch.run(mode="monthly")

        result = summary.services["fake"]
        assert result.discovered == 3
        assert result.targets == 3
        assert len(result.exported) == 3
        assert result.failed == []
        assert all(s.closed for s in sessions)

        json_path = tmp / "exports" / "fake" / "conversation-1.json"
        html_path = tmp / "exports" / "fake" / "conversation-1.html"
        assert json_path.exists() and html_path.exists()
        assert "c01" in html_path.read_text(encoding="utf-8")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["platform"] == "fake"
        assert data["conversation_id"] == "c01"
        assert data["messages"][0]["role"] == "user"
        assert "texte" in data["messages"][0]
        assert "code_blocks" in data["messages"][0]

        listing = read_list(tmp)
        assert listing["count"] == 3
        entry = next(c for c in listing["conversations"] if c["conversation_id"] == "c01")
        assert entry["scraped"] is True
        assert entry["message_count"] == 2
        assert entry["file"] == "conversation-1"

    def test_fold_insensible_accent_casse(self):
        assert _fold("Conversation Étalon") == "conversation etalon"

    def test_match_filtre_par_titre(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        result = orch.run(mode="monthly", match="conversation 2").services["fake"]
        assert result.targets == 1
        assert len(result.exported) == 1
        assert result.exported[0].stem == "conversation-2"

    def test_screenshots_par_message(self, workdir):
        orch, _ = make_orchestrator(
            workdir, {"fake": ScreenshotService}, {"screenshots": True}
        )
        summary = orch.run(mode="monthly")
        assert len(summary.services["fake"].exported) == 3
        shots = sorted(
            (workdir / "exports" / "fake" / "screenshots").glob("*/message-01.png")
        )
        assert len(shots) == 3

    def test_screenshots_desactives_par_defaut(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": ScreenshotService})
        orch.run(mode="monthly")
        assert not (workdir / "exports" / "fake" / "screenshots").exists()

    def test_match_sans_resultat(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        result = orch.run(mode="monthly", match="introuvable").services["fake"]
        assert result.targets == 0
        assert result.exported == []

    def test_daily_sans_inventaire_scrape_tout(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        summary = orch.run(mode="daily")
        assert len(summary.services["fake"].exported) == 3

    def test_daily_scrape_inconnues_et_20_recentes(self, workdir):
        tmp = workdir
        # 1er passage monthly : tout est marque scrape
        orch, _ = make_orchestrator(tmp, {"fake": ManyConversations})
        orch.run(mode="monthly")
        assert read_list(tmp)["count"] == 25

        # daily : les 20 plus recentes (ordre sidebar) sont traitees
        # (contenu identique -> unchanged, pas de reecriture)
        orch2, _ = make_orchestrator(tmp, {"fake": ManyConversations})
        res = orch2.run(mode="daily").services["fake"]
        assert len(res.exported) + len(res.unchanged) == 20
        assert len(res.unchanged) == 20
        # les 5 plus anciennes ne sont pas retouchees
        assert "c25" not in res.unchanged and "c25" not in res.exported

    def test_daily_ajoute_les_nouvelles(self, workdir):
        tmp = workdir
        orch, _ = make_orchestrator(tmp, {"fake": FakeService})
        orch.run(mode="monthly")
        # nouvelle conversation ajoutee a la liste source
        class MoreRefs(FakeService):
            name = "fake"
            ref_count = 4

        orch2, _ = make_orchestrator(tmp, {"fake": MoreRefs})
        result = orch2.run(mode="daily").services["fake"]
        assert "conversation-4" in {p.stem for p in result.exported}  # nouvelle
        assert "c01" in result.unchanged  # deja presente, non reecrite

    def test_contenu_identique_non_reecrit(self, workdir):
        tmp = workdir
        orch, _ = make_orchestrator(tmp, {"fake": FakeService})
        first = orch.run(mode="monthly")
        path = first.services["fake"].exported[0]
        before = path.read_text(encoding="utf-8")
        orch2, _ = make_orchestrator(tmp, {"fake": FakeService})
        second = orch2.run(mode="monthly")
        # contenu identique -> aucune reecriture en masse
        assert second.services["fake"].exported == []
        assert len(second.services["fake"].unchanged) == 3
        assert path.read_text(encoding="utf-8") == before

    def test_nouveaux_messages_patches(self, workdir):
        tmp = workdir

        class GrowingService(FakeService):
            name = "growing"
            extra = False

            def export_conversation_with_html(self, ref):
                n = 3 if GrowingService.extra else 2
                return make_conversation("growing", ref.id, n=n, title=ref.title), "<html/>"

        orch, _ = make_orchestrator(tmp, {"growing": GrowingService})
        orch.run(mode="monthly")
        path = tmp / "exports" / "growing" / "conversation-1.json"
        assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == 2

        GrowingService.extra = True
        orch2, _ = make_orchestrator(tmp, {"growing": GrowingService})
        summary = orch2.run(mode="monthly")
        result = summary.services["growing"]
        assert result.patched == ["c01", "c02", "c03"]
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["messages"]) == 3
        # les anciens messages sont conserves (prefixe intact)
        assert data["messages"][0]["texte"] == "q0"

    def test_collision_de_titres_desambiguee(self, workdir):
        tmp = workdir

        class SameTitle(FakeService):
            name = "fake"
            def export_conversation_with_html(self, ref):
                return make_conversation("fake", ref.id, title="Meme titre"), "<html/>"

        orch, _ = make_orchestrator(tmp, {"fake": SameTitle})
        orch.run(mode="monthly")
        files = sorted(
            p.stem for p in (tmp / "exports" / "fake").glob("*.json")
            if p.name != "conversation_list.json"
        )
        assert len(files) == 3
        assert len(set(files)) == 3

    def test_limit(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        summary = orch.run(mode="monthly", limit=2)
        assert len(summary.services["fake"].exported) == 2

    def test_conversation_en_echec_n_arrete_pas_le_service(self, workdir):
        orch, _ = make_orchestrator(workdir, {"flaky": ExplodingConversation})
        summary = orch.run(mode="monthly")
        result = summary.services["flaky"]
        assert len(result.exported) == 2
        assert result.failed == ["c02"]
        assert summary.has_failures

    def test_discovery_en_echec(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"boom": ExplodingDiscovery})
        summary = orch.run(mode="monthly")
        result = summary.services["boom"]
        assert result.failed
        assert any("sidebar" in e for e in result.failed)
        assert sessions[0].closed

    def test_auth_requise_skip_service(self, workdir):
        orch, _ = make_orchestrator(workdir, {"authlost": AuthLostService})
        summary = orch.run(mode="monthly")
        result = summary.services["authlost"]
        assert result.skipped_reason
        assert "session expiree" in result.skipped_reason
        assert summary.has_failures

    def test_services_desactives_sautes(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        orch.config["services"]["fake"]["enabled"] = False
        summary = orch.run(mode="monthly")
        # service desactive -> absent du run
        assert "fake" not in summary.services
        assert not summary.has_failures
        assert summary.total_exported == 0

    def test_run_service_desactive_retourne_skip(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        orch.config["services"]["fake"]["enabled"] = False
        result = orch.run_service("fake", mode="monthly")
        assert result.skipped_reason == "disabled"

    def test_service_inconnu_ignore(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        result = orch.run_service("ghost", mode="monthly")
        assert result.skipped_reason == "unknown service"

    def test_config_url_surcharge_home(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        orch.config["services"]["fake"]["url"] = "https://autre.test/"
        orch.run(mode="monthly")
        assert FakeService.instances[-1].home_url == "https://autre.test/"

    def test_mode_invalide(self, workdir):
        orch, _ = make_orchestrator(workdir, {"fake": FakeService})
        with pytest.raises(ValueError, match="mode invalide"):
            orch.run(mode="weekly")


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

    def test_auto_bascule_botasaurus_sur_discovery_bloquee(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"blockdisc": BlockedDiscoveryService})
        summary = orch.run(mode="monthly")
        result = summary.services["blockdisc"]
        assert len(result.exported) == 3
        assert result.failed == []
        assert len(sessions) == 2
        assert sessions[0].config.get("_engine") == "playwright"
        assert sessions[1].config.get("_engine") == "botasaurus"

    def test_auto_bascule_botasaurus_sur_conversation_bloquee(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"blockconv": BlockedOnceConversation})
        summary = orch.run(mode="monthly")
        result = summary.services["blockconv"]
        assert len(result.exported) == 3
        assert result.failed == []
        assert len(sessions) == 2
        assert sessions[1].config.get("_engine") == "botasaurus"

    def test_bloquage_persistant_skip_service(self, workdir):
        orch, sessions = make_orchestrator(workdir, {"hardblock": AlwaysBlockedDiscovery})
        summary = orch.run(mode="monthly")
        result = summary.services["hardblock"]
        assert result.skipped_reason
        assert "challenge" in result.skipped_reason
        assert len(sessions) == 2

    def test_engine_botasaurus_direct_sans_fallback(self, workdir):
        config_extra = {"engine": "botasaurus"}
        orch, sessions = make_orchestrator(
            workdir, {"hardblock": AlwaysBlockedDiscovery}, config_extra
        )
        summary = orch.run(mode="monthly")
        result = summary.services["hardblock"]
        assert result.skipped_reason
        assert len(sessions) == 1
        assert sessions[0].config.get("_engine") == "botasaurus"

    def test_conversation_vide_ignoree_pas_un_echec(self, workdir):
        orch, _ = make_orchestrator(workdir, {"emptyconv": EmptyConversationService})
        summary = orch.run(mode="monthly")
        result = summary.services["emptyconv"]
        assert sorted(result.skipped) == ["c01", "c02", "c03"]
        assert result.failed == []
        assert not summary.has_failures
        assert result.exported == []


class TestRunSummary:
    def test_ok_et_total(self):
        summary = RunSummary(mode="daily")
        summary.services["a"] = ServiceResult(service="a", exported=[Path("/x/a.json")])
        assert summary.total_exported == 1
        assert not summary.has_failures

    def test_service_result_ok(self):
        assert ServiceResult(service="a").ok
        assert not ServiceResult(service="a", failed=["x"]).ok


class TestFingerprint:
    def test_deterministe_et_persiste(self, tmp_path):
        from src.fingerprint import generate_fingerprint, get_fingerprint

        assert generate_fingerprint("chatgpt") == generate_fingerprint("chatgpt")
        profile = tmp_path / "profiles" / "grok"
        fp = get_fingerprint("grok", profile)
        assert (profile / "fingerprint.json").exists()
        assert get_fingerprint("grok", profile) == fp
        assert fp["user_agent"] and fp["window_width"] > 0

    def test_isole_par_service(self):
        from src.fingerprint import generate_fingerprint

        fingerprints = [
            generate_fingerprint(s)
            for s in ("chatgpt", "claude", "gemini", "perplexity", "grok", "mistral")
        ]
        # au moins deux profils differents (profils isoles)
        distinct = {f["user_agent"] for f in fingerprints}
        assert len(distinct) >= 2


class TestParallelism:
    def test_groupes_par_domaine(self, workdir):
        class SvcA(FakeService):
            name = "svca"

        class SvcB(FakeService):
            name = "svcb"

        orch, _ = make_orchestrator(workdir, {"svca": SvcA, "svcb": SvcB})
        orch.config["services"] = {
            "svca": {"enabled": True, "url": "https://a.test/"},
            "svcb": {"enabled": True, "url": "https://a.test/x"},  # meme domaine
        }
        groups = orch._group_by_domain(["svca", "svcb"])
        assert len(groups) == 1 and sorted(groups[0]) == ["svca", "svcb"]

        orch.config["services"]["svcb"]["url"] = "https://b.test/"
        groups = orch._group_by_domain(["svca", "svcb"])
        assert len(groups) == 2

    def test_run_parallele_exporte_les_deux(self, workdir):
        class SvcA(FakeService):
            name = "svca"

        class SvcB(FakeService):
            name = "svcb"

        orch, sessions = make_orchestrator(
            workdir,
            {"svca": SvcA, "svcb": SvcB},
            {"services": {"svca": {"enabled": True, "url": "https://a.test/"},
                          "svcb": {"enabled": True, "url": "https://b.test/"}}},
        )
        summary = orch.run(mode="monthly", parallel=2)
        assert len(summary.services["svca"].exported) == 3
        assert len(summary.services["svcb"].exported) == 3
        assert all(s.closed for s in sessions)


class TestSmartJson:
    def test_identique_non_ecrit(self):
        from src.utils.file_utils import merge_conversation_json

        obj = {"conversation_id": "c", "messages": [{"role": "user", "texte": "a"}],
               "exported_at": "old"}
        new = dict(obj, exported_at="new")
        _, status, _ = merge_conversation_json(obj, new)
        assert status == "unchanged"

    def test_nouveaux_messages_patch(self):
        from src.utils.file_utils import merge_conversation_json

        old = {"conversation_id": "c", "messages": [{"texte": "a"}, {"texte": "b"}],
               "exported_at": "v1"}
        new = {"conversation_id": "c",
               "messages": [{"texte": "a"}, {"texte": "b"}, {"texte": "c"}],
               "exported_at": "v2"}
        merged, status, appended = merge_conversation_json(old, new)
        assert status == "patched" and appended == 1
        assert [m["texte"] for m in merged["messages"]] == ["a", "b", "c"]

    def test_contenu_divergent_reecrit(self):
        from src.utils.file_utils import merge_conversation_json

        old = {"conversation_id": "c", "messages": [{"texte": "a"}], "exported_at": "v1"}
        new = {"conversation_id": "c", "messages": [{"texte": "X"}], "exported_at": "v2"}
        _, status, _ = merge_conversation_json(old, new)
        assert status == "rewritten"

    def test_premier_ecrit(self):
        from src.utils.file_utils import merge_conversation_json

        new = {"conversation_id": "c", "messages": [{"texte": "a"}]}
        _, status, _ = merge_conversation_json(None, new)
        assert status == "written"
