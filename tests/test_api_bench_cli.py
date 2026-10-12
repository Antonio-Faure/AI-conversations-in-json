"""Tests du CLI scripts/api_bench.py (commandes hors ligne : shape, update-ref)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.utils.api_shape import shape_of, signature_of

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "api_bench_cli", ROOT / "scripts" / "api_bench.py"
)
api_bench = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("api_bench_cli", api_bench)
spec.loader.exec_module(api_bench)

PAYLOAD = {
    "conversation_id": "c01",
    "title": "Titre",
    "messages": [
        {"id": "m1", "author": {"role": "user"}, "content": "bonjour"}
    ],
}
DRIFT_LEGER = {
    **PAYLOAD,
    "nouveau_champ": 3,
}
DRIFT_FORT = {"autre": [1, 2, 3]}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Config factice + dossier de references dans tmp_path."""
    shapes = tmp_path / "shapes"
    monkeypatch.setattr(api_bench, "SHAPES_DIR", shapes)
    monkeypatch.setattr(
        api_bench,
        "load_config",
        lambda path: {
            "output_dir": str(tmp_path / "exports"),
            "profile_dir": str(tmp_path / "profiles"),
            "cookies_dir": str(tmp_path / "cookies"),
        },
    )
    return tmp_path


def write_payload(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestParser:
    def test_shape_from_et_live_exclusifs(self):
        with pytest.raises(SystemExit):
            api_bench.build_parser().parse_args(
                ["shape", "--platform", "chatgpt", "--from", "x.json", "--live"]
            )

    def test_bench_n(self):
        args = api_bench.build_parser().parse_args(
            ["bench", "--platform", "grok", "--n", "3", "--json"]
        )
        assert args.n == 3 and args.json is True

    def test_plateforme_inconnue(self):
        with pytest.raises(SystemExit):
            api_bench.build_parser().parse_args(["shape", "--platform", "copilot"])

    def test_commande_requise(self):
        with pytest.raises(SystemExit):
            api_bench.build_parser().parse_args([])


class TestUpdateRef:
    def test_ecrit_la_reference_depuis_un_payload(self, env, capsys):
        payload_path = write_payload(env, "payload.json", PAYLOAD)
        code = api_bench.main(
            ["update-ref", "--platform", "chatgpt", "--from", str(payload_path)]
        )
        assert code == 0
        doc = json.loads(
            (env / "shapes" / "chatgpt.shape.json").read_text(encoding="utf-8")
        )
        assert doc["platform"] == "chatgpt"
        assert doc["scope"] == "from"
        assert doc["sources"] == 1
        assert doc["shape"] == shape_of(PAYLOAD)
        assert doc["signature"] == signature_of(PAYLOAD)
        assert "reference ecrite" in capsys.readouterr().out

    def test_union_du_corpus(self, env, capsys):
        raw = env / "exports" / "chatgpt" / "raw"
        raw.mkdir(parents=True)
        write_payload(raw, "a.json", PAYLOAD)
        write_payload(raw, "b.json", {**PAYLOAD, "extra": {"x": 1}})
        code = api_bench.main(["update-ref", "--platform", "chatgpt", "--all"])
        assert code == 0
        doc = json.loads(
            (env / "shapes" / "chatgpt.shape.json").read_text(encoding="utf-8")
        )
        assert doc["scope"] == "corpus"
        assert doc["sources"] == 2
        # l'union couvre les deux payloads
        assert doc["shape"]["c"]["extra"]["t"] == "object"

    def test_etalons_absents_exit_2(self, env, capsys):
        code = api_bench.main(["update-ref", "--platform", "chatgpt"])
        assert code == 2
        assert "introuvables" in capsys.readouterr().err

    def test_payload_illisible(self, env, capsys):
        code = api_bench.main(
            ["update-ref", "--platform", "chatgpt", "--from", str(env / "absent.json")]
        )
        assert code == 2
        assert "illisible" in capsys.readouterr().err

    def test_from_et_all_exclusifs(self, env):
        with pytest.raises(SystemExit):
            api_bench.build_parser().parse_args(
                ["update-ref", "--platform", "chatgpt", "--from", "x.json", "--all"]
            )


class TestShape:
    def _reference(self, env):
        write_payload(env, "payload.json", PAYLOAD)
        api_bench.main(
            ["update-ref", "--platform", "chatgpt",
             "--from", str(env / "payload.json")]
        )

    def test_identique_score_100(self, env, capsys):
        self._reference(env)
        code = api_bench.main(
            ["shape", "--platform", "chatgpt", "--from", str(env / "payload.json")]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "score=100.0% OK" in out
        assert "aucun ecart de forme" in out

    def test_drift_leger_warn_exit_0(self, env, capsys):
        self._reference(env)
        drifted = write_payload(env, "drift.json", DRIFT_LEGER)
        code = api_bench.main(
            ["shape", "--platform", "chatgpt", "--from", str(drifted)]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "WARN" in out
        assert "$.nouveau_champ" in out

    def test_drift_fort_break_exit_1(self, env, capsys):
        self._reference(env)
        drifted = write_payload(env, "break.json", DRIFT_FORT)
        code = api_bench.main(
            ["shape", "--platform", "chatgpt", "--from", str(drifted)]
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "BREAK" in out

    def test_json(self, env, capsys):
        self._reference(env)
        capsys.readouterr()  # purge la sortie de update-ref
        code = api_bench.main(
            ["shape", "--platform", "chatgpt", "--from",
             str(env / "payload.json"), "--json"]
        )
        assert code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["score"] == 100.0
        assert report["signature_ref"] == report["signature_live"]

    def test_reference_absente_exit_2(self, env, capsys):
        payload = write_payload(env, "payload.json", PAYLOAD)
        code = api_bench.main(
            ["shape", "--platform", "chatgpt", "--from", str(payload)]
        )
        assert code == 2
        assert "reference de forme absente" in capsys.readouterr().err

    def test_bench_sans_reference_exit_2(self, env, capsys):
        code = api_bench.main(["bench", "--platform", "chatgpt", "--n", "1"])
        assert code == 2
        assert "reference de forme absente" in capsys.readouterr().err


class TestReport:
    def test_agrege_api_health(self, env, capsys):
        health_dir = env / "exports" / "chatgpt"
        health_dir.mkdir(parents=True)
        (health_dir / "api_health.json").write_text(
            json.dumps(
                {
                    "platform": "chatgpt",
                    "n": 42,
                    "mean_score": 98.7,
                    "min_score": 88.1,
                    "drifts": 2,
                    "breaks": 0,
                    "updated_at": "2026-10-12T10:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        code = api_bench.main(["report", "--json"])
        assert code == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows["chatgpt"]["mean_score"] == 98.7
        assert rows["grok"] is None

    def test_table(self, env, capsys):
        code = api_bench.main(["report"])
        out = capsys.readouterr().out
        assert code == 0
        assert "chatgpt" in out and "(aucun rapport)" in out
