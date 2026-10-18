"""Tests du serveur MCP scripts/mcp_conversations.py.

Unitaires (handlers, hors protocole, sans modele d'embeddings) + handshake
stdio reel via le SDK `mcp` (initialize, tools/list, tools/call).

Le test live `search` ne tourne que si `AICV_ROOT` pointe vers un depot avec
un index RAG existant (voir la fin du fichier).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from src.rag.store import VectorStore

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "mcp_conversations.py"
spec = importlib.util.spec_from_file_location("mcp_conversations", SCRIPT)
mcp_conversations = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("mcp_conversations", mcp_conversations)
spec.loader.exec_module(mcp_conversations)


class FakeEmbedder:
    """Vecteur fixe : seuls les scores semantiques triviaux sont testes."""

    model_name = "fake-model"

    def encode_one(self, text):
        return np.array([1.0, 0.0, 0.0, 0.0], dtype="float32")


def _message(mid, role, texte, **extra):
    message = {
        "conversation_id": extra.pop("conversation_id", ""),
        "message_id": mid,
        "role": role,
        "platform": "alpha",
        "model": "modele-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "texte": texte,
        "code_blocks": [],
        "reasoning": "",
        "artifacts": [],
        "sources": [],
        "tools": [],
        "variants": [],
    }
    message.update(extra)
    return message


def _conversation(cid, platform, title, messages, **extra):
    payload = {
        "conversation_id": cid,
        "platform": platform,
        "title": title,
        "url": f"https://{platform}.example/{cid}",
        "model": "modele-1",
        "started_at": "2026-01-01T00:00:00Z",
        "last_message_at": "2026-01-02T00:00:00Z",
        "exported_at": "2026-02-01T00:00:00Z",
        "messages": messages,
    }
    payload.update(extra)
    return payload


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def env(tmp_path):
    """Arborescence exports factice + store RAG (dim 4) + tools prets a l'emploi."""
    exports = tmp_path / "exports"
    alpha = exports / "alpha"
    beta = exports / "beta"

    _write(
        alpha / "conversation_list.json",
        {
            "platform": "alpha",
            "updated_at": "2026-02-03T00:00:00Z",
            "count": 3,
            "conversations": [
                {
                    "conversation_id": "conv-c1-2026",
                    "title": "Serveur minecraft",
                    "url": "https://alpha.example/c1",
                    "message_count": 2,
                    "last_message_at": "2026-01-02T00:00:00Z",
                    "has_code": False,
                    "scraped": True,
                    "file": "serveur-minecraft",
                    "scraped_at": "2026-02-01T00:00:00Z",
                },
                {
                    "conversation_id": "conv-c2",
                    "title": "Recette de crêpes",
                    "url": "https://alpha.example/c2",
                    "message_count": 2,
                    "last_message_at": "2026-03-05T00:00:00Z",
                    "has_code": True,
                    "scraped": True,
                    "file": "recette-de-crepes",
                    "scraped_at": "2026-02-02T00:00:00Z",
                },
                {
                    "conversation_id": "conv-c3",
                    "title": "Vieux sujet sur les tulipes",
                    "url": "https://alpha.example/c3",
                    "message_count": None,
                    "last_message_at": None,
                    "has_code": None,
                    "scraped": False,
                    "file": None,
                    "scraped_at": None,
                    "unavailable": True,
                },
            ],
        },
    )
    _write(
        alpha / "serveur-minecraft.json",
        _conversation(
            "conv-c1-2026",
            "alpha",
            "Serveur minecraft",
            [
                _message("m1", "user", "Comment configurer un serveur Minecraft ?"),
                _message(
                    "m2",
                    "assistant",
                    "Édite le fichier server.properties puis lance le serveur.",
                    reasoning="verifier la mémoire allouée",
                    artifacts=["config-notes"],
                    sources=["Wiki — https://wiki.example/server.properties"],
                    tools=["Recherche web"],
                    variants=[
                        {
                            "kind": "regeneration",
                            "texte": "Ancienne réponse sur le serveur",
                            "code_blocks": [],
                            "model": None,
                            "timestamp": None,
                            "message_id": "",
                            "active": False,
                        }
                    ],
                ),
            ],
        ),
    )
    _write(
        alpha / "recette-de-crepes.json",
        _conversation(
            "conv-c2",
            "alpha",
            "Recette de crêpes",
            [
                _message("m3", "user", "Donne-moi une recette de crêpes"),
                _message(
                    "m4",
                    "assistant",
                    "Mélange la farine et les œufs.\n\n```python\nprint('crepes')\n```",
                ),
            ],
        ),
    )
    _write(
        beta / "conversation_list.json",
        {
            "platform": "beta",
            "updated_at": "2026-02-03T00:00:00Z",
            "count": 1,
            "conversations": [
                {
                    "conversation_id": "conv-c9",
                    "title": "Autre sujet",
                    "url": "https://beta.example/c9",
                    "message_count": 1,
                    "last_message_at": "2026-04-01T00:00:00Z",
                    "has_code": False,
                    "scraped": True,
                    "file": "autre-sujet",
                    "scraped_at": "2026-02-03T00:00:00Z",
                }
            ],
        },
    )
    _write(
        beta / "autre-sujet.json",
        _conversation(
            "conv-c9",
            "beta",
            "Autre sujet",
            [_message("m9", "user", "Parle-moi d'autre chose", conversation_id="conv-c9")],
        ),
    )

    db = tmp_path / "rag" / "messages.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = VectorStore(db, dim=4)
    store.replace_conversation(
        "alpha", "conv-c1-2026", "Serveur minecraft", "2026-02-01T00:00:00Z",
        [
            _message("m1", "user", "Comment configurer un serveur Minecraft ?"),
            _message("m2", "assistant", "Édite le fichier server.properties."),
        ],
        np.array([[1.0, 0, 0, 0], [0.0, 1, 0, 0]], dtype="float32"),
        url="https://alpha.example/c1",
    )
    store.replace_conversation(
        "alpha", "conv-c2", "Recette de crêpes", "2026-02-02T00:00:00Z",
        [_message("m3", "user", "Donne-moi une recette de crêpes")],
        np.array([[0.0, 0, 1, 0]], dtype="float32"),
        url="https://alpha.example/c2",
    )
    store.replace_conversation(
        "beta", "conv-c9", "Autre sujet", "2026-02-03T00:00:00Z",
        [_message("m9", "user", "Parle-moi d'autre chose")],
        np.array([[1.0, 0, 0, 0]], dtype="float32"),
        url="https://beta.example/c9",
    )

    config = {
        "output_dir": str(exports),
        "rag_db": str(db),
        "rag_model": "fake-model",
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "output_dir": "exports",
                "rag_db": "rag/messages.db",
                "rag_model": "fake-model",
            }
        ),
        encoding="utf-8",
    )
    tools = mcp_conversations.ConversationTools(
        config, store=store, embedder=FakeEmbedder()
    )
    yield SimpleNamespace(
        tmp_path=tmp_path,
        exports=exports,
        db=db,
        config=config,
        config_path=config_path,
        tools=tools,
        store=store,
    )
    store.close()


# -- search ------------------------------------------------------------------------


def test_search_groupes_et_filtres(env):
    result = env.tools.search("minecraft", k=5)
    assert result["count"] >= 1
    platforms = {g["platform"] for g in result["groups"]}
    assert "alpha" in platforms
    alpha_group = next(g for g in result["groups"] if g["platform"] == "alpha")
    assert alpha_group["conversation_id"] == "conv-c1-2026"
    assert alpha_group["title"] == "Serveur minecraft"
    assert alpha_group["url"] == "https://alpha.example/c1"
    first = alpha_group["messages"][0]
    assert first["message_id"] == "m1"
    assert first["index"] == 1
    assert first["role"] == "user"
    assert "Minecraft" in first["snippet"]
    assert first["score"] > 0.5

    only_beta = env.tools.search("minecraft", platforms=["beta"])
    assert [g["platform"] for g in only_beta["groups"]] == ["beta"]

    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.search("")
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.search("minecraft", platforms=["nope"])


def test_search_index_rag_absent_erreur_claire(tmp_path):
    tools = mcp_conversations.ConversationTools(
        {"output_dir": str(tmp_path / "exports"), "rag_db": str(tmp_path / "absent.db")}
    )
    with pytest.raises(mcp_conversations.ConversationToolError) as exc:
        tools.search("minecraft")
    assert "rag_index.py" in str(exc.value)


# -- grep -------------------------------------------------------------------------


def test_grep_litteral_champs_et_snippets(env):
    result = env.tools.grep("minecraft")
    assert result["count"] >= 2
    fields = {m["field"] for m in result["matches"]}
    assert "texte" in fields
    assert "title" in fields  # le titre de la conversation matche aussi
    message_hit = next(m for m in result["matches"] if m["field"] == "texte")
    assert message_hit["platform"] == "alpha"
    assert message_hit["conversation_id"] == "conv-c1-2026"
    assert message_hit["message_index"] == 1
    assert message_hit["message_id"] == "m1"
    assert "Minecraft" in message_hit["snippet"]

    # insensible a la casse et present dans les extras (raisonnement)
    assert env.tools.grep("VERIFIER LA MÉMOIRE")["count"] == 1
    assert env.tools.grep("wiki.example")["count"] >= 1
    assert env.tools.grep("ancienne réponse")["count"] == 1


def test_grep_regex_plateforme_et_limite(env):
    regex = env.tools.grep(r"server\.[a-z]+", regex=True)
    assert regex["regex"] is True
    assert regex["count"] >= 1
    assert any("server.properties" in m["snippet"] for m in regex["matches"])

    alpha_only = env.tools.grep("recette", platform="alpha")
    assert alpha_only["count"] >= 1
    assert {m["platform"] for m in alpha_only["matches"]} == {"alpha"}

    limited = env.tools.grep("e", limit=2)
    assert limited["count"] == 2
    assert limited["truncated"] is True


def test_grep_liste_des_conversations_non_scrapees(env):
    result = env.tools.grep("tulipes")
    assert result["count"] == 1
    hit = result["matches"][0]
    assert hit["field"] == "list.title"
    assert hit["conversation_id"] == "conv-c3"
    assert hit["unavailable"] is True
    assert "message_index" not in hit


def test_grep_erreurs(env):
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.grep("")
    with pytest.raises(mcp_conversations.ConversationToolError) as exc:
        env.tools.grep("(unbalanced", regex=True)
    assert "regex invalide" in str(exc.value)
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.grep("x", platform="nope")


# -- get_conversation / get_message -------------------------------------------------


def test_get_conversation_complet_et_tronque(env):
    full = env.tools.get_conversation("alpha", "conv-c1-2026")
    assert full["truncated"] is False
    assert full["total_messages"] == 2
    assert [m["message_id"] for m in full["conversation"]["messages"]] == ["m1", "m2"]
    assert full["conversation"]["title"] == "Serveur minecraft"

    partial = env.tools.get_conversation("alpha", "conv-c1-2026", max_messages=1)
    assert partial["truncated"] is True
    assert partial["total_messages"] == 2
    assert len(partial["conversation"]["messages"]) == 1

    prefixed = env.tools.get_conversation("alpha", "conv-c1")
    assert prefixed["conversation"]["conversation_id"] == "conv-c1-2026"
    with pytest.raises(mcp_conversations.ConversationToolError) as exc:
        env.tools.get_conversation("alpha", "conv-c")
    assert "ambigu" in str(exc.value)
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.get_conversation("alpha", "zzz")
    with pytest.raises(mcp_conversations.ConversationToolError) as exc:
        env.tools.get_conversation("alpha", "conv-c3")
    assert "non scrapee" in str(exc.value)


def test_get_message_index_et_message_id(env):
    by_index = env.tools.get_message("alpha", "conv-c1-2026", "2")
    assert by_index["index"] == 2
    assert by_index["matched_by"] == "index"
    assert by_index["message"]["message_id"] == "m2"
    assert by_index["message"]["reasoning"] == "verifier la mémoire allouée"
    assert by_index["message"]["sources"] == ["Wiki — https://wiki.example/server.properties"]

    by_id = env.tools.get_message("alpha", "conv-c1-2026", "m1")
    assert by_id["index"] == 1
    assert by_id["matched_by"] == "message_id"

    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.get_message("alpha", "conv-c1-2026", "9")
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.get_message("alpha", "conv-c1-2026", "mX")


# -- list_conversations / stats ------------------------------------------------------


def test_list_conversations_tri_et_limite(env):
    recent = env.tools.list_conversations()
    assert recent["total"] == 4
    assert recent["conversations"][0]["conversation_id"] == "conv-c9"  # 2026-04-01
    assert recent["conversations"][-1]["conversation_id"] == "conv-c3"  # sans date
    assert recent["conversations"][-1]["unavailable"] is True

    by_title = env.tools.list_conversations(sort="title", limit=1)
    assert by_title["truncated"] is True
    assert by_title["count"] == 1

    by_messages = env.tools.list_conversations(platform="alpha", sort="messages")
    assert by_messages["total"] == 3
    assert by_messages["conversations"][0]["message_count"] == 2

    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.list_conversations(sort="nope")


def test_stats_compteurs(env):
    result = env.tools.stats()
    assert result["rag"]["available"] is True
    assert result["rag"]["messages"] == 4
    assert result["rag"]["conversations"] == 3
    assert result["rag"]["per_platform"]["alpha"]["conversations"] == 2
    assert result["exports"]["per_platform"]["alpha"]["listed"] == 3
    assert result["exports"]["per_platform"]["alpha"]["scraped"] == 2
    assert result["exports"]["per_platform"]["alpha"]["unavailable"] == 1
    assert result["exports"]["per_platform"]["alpha"]["files"] == 2
    assert result["exports"]["total_files"] == 3


def test_stats_sans_index_rag(tmp_path):
    tools = mcp_conversations.ConversationTools(
        {"output_dir": str(tmp_path / "exports"), "rag_db": str(tmp_path / "absent.db")}
    )
    result = tools.stats()
    assert result["rag"]["available"] is False
    assert "rag_index.py" in result["rag"]["error"]


def test_stats_ignore_les_json_non_conversation(env):
    """`api_health.json` (comme `conversation_list.json`) n'est pas un fichier de conversation."""
    (env.exports / "alpha" / "api_health.json").write_text(
        json.dumps({"platform": "alpha", "n": 3, "mean_score": 100.0}),
        encoding="utf-8",
    )
    result = env.tools.stats()
    assert result["exports"]["per_platform"]["alpha"]["files"] == 2
    assert result["exports"]["total_files"] == 3


def test_get_conversation_fichier_corrompu_erreur_detaillee(env):
    """Un JSON de conversation corrompu -> erreur claire (chemin + cause)."""
    (env.exports / "alpha" / "serveur-minecraft.json").write_text(
        "{corrompu", encoding="utf-8"
    )
    with pytest.raises(mcp_conversations.ConversationToolError) as exc:
        env.tools.get_conversation("alpha", "conv-c1-2026")
    message = str(exc.value)
    assert "fichier illisible" in message
    assert "serveur-minecraft.json" in message


# -- config ------------------------------------------------------------------------


def test_config_absente_message_clair(tmp_path, capsys):
    """`-c <fichier inexistant>` : message explicite (pas de traceback)."""
    code = mcp_conversations.main(["--config", str(tmp_path / "absent.yaml")])
    assert code == 2
    assert "introuvable" in capsys.readouterr().err


def test_config_chemins_resolus_depuis_le_fichier(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"output_dir": "mes-exports", "rag_db": "mon-rag.db"}),
        encoding="utf-8",
    )
    config = mcp_conversations.load_server_config(config_path)
    assert config["output_dir"] == str(tmp_path / "mes-exports")
    assert config["rag_db"] == str(tmp_path / "mon-rag.db")


# -- handshake stdio reel (SDK mcp) --------------------------------------------------


def _run_async(coro):
    return asyncio.run(coro)


def test_handshake_stdio_et_appels_outils(env):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SCRIPT), "--config", str(env.config_path)],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "aicv"

                listed = await session.list_tools()
                names = sorted(tool.name for tool in listed.tools)
                assert names == [
                    "get_conversation",
                    "get_message",
                    "grep",
                    "list_conversations",
                    "recheck_parsers",
                    "recheck_status",
                    "scrape_conversations",
                    "scrape_status",
                    "search",
                    "stats",
                ]
                search_tool = next(t for t in listed.tools if t.name == "search")
                assert "query" in search_tool.input_schema["properties"]
                recheck_tool = next(
                    t for t in listed.tools if t.name == "recheck_parsers"
                )
                assert "per_platform" in recheck_tool.input_schema["properties"]

                grep = await session.call_tool("grep", {"pattern": "minecraft"})
                assert grep.is_error is False
                payload = json.loads(grep.content[0].text)
                assert payload["count"] >= 1

                stats = await session.call_tool("stats", {})
                stats_payload = json.loads(stats.content[0].text)
                # le config passe par le fichier : chemins resolus depuis son dossier
                assert stats_payload["rag"]["available"] is True
                assert stats_payload["rag"]["messages"] == 4

                broken = await session.call_tool(
                    "get_conversation",
                    {"platform": "nope", "conversation_id": "x"},
                )
                assert broken.is_error is True
                assert "plateforme inconnue" in broken.content[0].text

    _run_async(scenario())


# -- validation live (index RAG reel) ------------------------------------------------


AICV_ROOT = Path(os.environ["AICV_ROOT"]) if os.environ.get("AICV_ROOT") else None
LIVE_READY = bool(
    AICV_ROOT and (AICV_ROOT / "config.yaml").exists() and (AICV_ROOT / "rag" / "messages-eg2.db").exists()
)


@pytest.mark.skipif(
    not LIVE_READY,
    reason="index RAG reel absent (definir AICV_ROOT vers le depot)",
)
def test_search_live_retrait_gonflement_argile():
    config = mcp_conversations.load_server_config(AICV_ROOT / "config.yaml")
    tools = mcp_conversations.ConversationTools(config)
    result = tools.search("retrait gonflement argile", k=20)
    ids = {g["conversation_id"] for g in result["groups"]}
    assert any(cid.startswith("34e0c27b") for cid in ids), sorted(ids)


# -- controle des parseurs (job injecte : aucun scrape pendant les tests) -----

def _install_fake_job(env, runner):
    from src.utils.recheck import RecheckJob

    env.tools._recheck_job = RecheckJob(
        runner=runner,
        lock_path=str(env.tmp_path / "run.lock"),
        orchestrator_factory=lambda config: object(),
    )
    return env.tools._recheck_job


def _ok_report(platform):
    return {
        "platform": platform, "verdict": "ok", "retention": 1.0,
        "checked": 1, "compared": 1, "ignored": 0, "ok": 1,
        "degraded": 0, "failed": 0, "checked_at": "2026-01-01T00:00:00Z",
        "conversations": [],
    }


def test_recheck_start_et_etat(env):
    seen = []

    def runner(config, platform, per_platform, min_messages, seed,
               orchestrator=None, log=print):
        seen.append((platform, per_platform, min_messages))
        return _ok_report(platform)

    job = _install_fake_job(env, runner)
    state = env.tools.recheck_start(["alpha"], per_platform=2, min_messages=1)
    assert state["started"] is True
    assert state["platforms"] == ["alpha"]
    job._thread.join(timeout=10)
    snapshot = env.tools.recheck_state()
    assert snapshot["running"] is False
    assert [item["platform"] for item in snapshot["done"]] == ["alpha"]
    assert seen == [("alpha", 2, 1)]


def test_recheck_toutes_plateformes_par_defaut(env):
    seen = []
    job = _install_fake_job(
        env,
        lambda config, platform, *a, **k: (seen.append(platform), _ok_report(platform))[1],
    )
    state = env.tools.recheck_start()
    assert state["platforms"] == ["alpha", "beta"]
    job._thread.join(timeout=10)
    assert seen == ["alpha", "beta"]


def test_recheck_plateforme_inconnue(env):
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.recheck_start(["inconnue"])


def test_recheck_wait_attend_la_fin(env):
    _install_fake_job(env, lambda *a, **k: None)
    state = env.tools.recheck_wait(["alpha"], per_platform=1, min_messages=1, timeout=10)
    assert state["running"] is False
    assert state["timed_out"] is False


def test_recheck_etat_lit_les_rapports(env):
    (env.exports / "alpha" / "recheck.json").write_text(
        json.dumps({
            "platform": "alpha", "verdict": "attention", "checked": 2,
            "compared": 2, "ok": 1, "degraded": 1, "failed": 0,
            "retention": 0.8, "checked_at": "2026-01-01T00:00:00Z",
            "conversations": [
                {"conversation_id": "c1", "title": "T", "status": "degrade",
                 "lost": ["images: 3 -> 1"], "retention": 0.8,
                 "old_messages": 5, "new_messages": 4},
                {"conversation_id": "c2", "title": "U", "status": "ok"},
            ],
        }),
        encoding="utf-8",
    )
    snapshot = env.tools.recheck_state()
    assert snapshot["running"] is False
    report = next(r for r in snapshot["reports"] if r["platform"] == "alpha")
    assert report["verdict"] == "attention"
    assert report["compared"] == 2
    assert len(report["issues"]) == 1
    assert report["issues"][0]["reason"] == "images: 3 -> 1"
    assert report["issues"][0]["old_messages"] == 5


# -- scrape a la demande (job injecte : aucun scrape pendant les tests) -------

def _install_fake_scrape_job(env, runner):
    from src.utils.scrape_job import ScrapeJob

    env.tools._scrape_job_instance = ScrapeJob(
        runner=runner,
        lock_path=str(env.tmp_path / "run.lock"),
        orchestrator_factory=lambda config: object(),
    )
    return env.tools._scrape_job_instance


def test_scrape_start_et_etat(env):
    seen = []

    def runner(config, platform, mode="daily", limit=None,
               orchestrator=None, log=print):
        seen.append((platform, mode, limit))
        return {
            "platform": platform, "mode": mode, "discovered": 5, "targets": 2,
            "exported": 1, "patched": 0, "failed": 0, "skipped": None, "ok": True,
        }

    job = _install_fake_scrape_job(env, runner)
    state = env.tools.scrape_start(["alpha"], mode="daily")
    assert state["started"] is True
    assert state["platforms"] == ["alpha"]
    job._thread.join(timeout=10)
    snapshot = env.tools.scrape_state()
    assert snapshot["running"] is False
    assert [item["platform"] for item in snapshot["done"]] == ["alpha"]
    assert snapshot["done"][0]["exported"] == 1
    assert seen == [("alpha", "daily", None)]


def test_scrape_mode_invalide(env):
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.scrape_start(["alpha"], mode="hebdo")


def test_scrape_plateforme_inconnue(env):
    with pytest.raises(mcp_conversations.ConversationToolError):
        env.tools.scrape_start(["inconnue"])


def test_scrape_wait_attend_la_fin(env):
    _install_fake_scrape_job(env, lambda *a, **k: None)
    state = env.tools.scrape_wait(["alpha"], timeout=10)
    assert state["running"] is False
    assert state["timed_out"] is False
