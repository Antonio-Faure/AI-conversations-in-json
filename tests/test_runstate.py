"""Tests de l'etat publie par `run.py` (scrape en cours, vu par la page web).

`run.py` (cron ou ligne de commande) ecrit `.state/aicv-run.json` pendant un
scrape : `read_run_state` doit rendre l'etat seulement s'il est encore vivant
(pid existant, fichier recent, processus `run.py`).
"""

from __future__ import annotations

import json
import os

from src.utils import runstate


def _path(tmp_path):
    return tmp_path / "aicv-run.json"


def test_ecriture_puis_lecture(tmp_path, monkeypatch):
    monkeypatch.setattr(runstate, "_looks_like_runner", lambda pid: True)
    path = _path(tmp_path)
    runstate.write_run_state("monthly", ["grok", "claude"], path=path)
    data = runstate.read_run_state(path=path)
    assert data is not None
    assert data["pid"] == os.getpid()
    assert data["mode"] == "monthly"
    assert data["services"] == ["grok", "claude"]
    assert data["started_at"].endswith("Z")


def test_mode_inconnu_ramene_a_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(runstate, "_looks_like_runner", lambda pid: True)
    path = _path(tmp_path)
    runstate.write_run_state("bidon", path=path)
    assert runstate.read_run_state(path=path)["mode"] == "daily"


def test_absent_ou_illisible(tmp_path):
    path = _path(tmp_path)
    assert runstate.read_run_state(path=path) is None
    path.write_text("pas du json", encoding="utf-8")
    assert runstate.read_run_state(path=path) is None
    path.write_text(json.dumps(["pas", "un", "objet"]), encoding="utf-8")
    assert runstate.read_run_state(path=path) is None


def test_pid_mort_ignore(tmp_path, monkeypatch):
    path = _path(tmp_path)
    runstate.write_run_state("daily", path=path)
    monkeypatch.setattr(runstate, "_pid_alive", lambda pid: False)
    assert runstate.read_run_state(path=path) is None


def test_etat_perime_ignore(tmp_path, monkeypatch):
    path = _path(tmp_path)
    runstate.write_run_state("daily", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["started_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(runstate, "_looks_like_runner", lambda pid: True)
    assert runstate.read_run_state(path=path) is None


def test_pid_reutilise_par_un_autre_programme(tmp_path, monkeypatch):
    path = _path(tmp_path)
    runstate.write_run_state("daily", path=path)
    monkeypatch.setattr(runstate, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(runstate, "_looks_like_runner", lambda pid: False)
    assert runstate.read_run_state(path=path) is None


def test_clear_seulement_le_sien(tmp_path):
    path = _path(tmp_path)
    runstate.write_run_state("daily", pid=111, path=path)
    runstate.clear_run_state(pid=222, path=path)  # un autre run a repris
    assert path.exists()
    runstate.clear_run_state(pid=111, path=path)
    assert not path.exists()
    runstate.clear_run_state(path=path)  # deja efface : sans erreur


def test_etat_fusionne_job_puis_run_externe(tmp_path, monkeypatch):
    """Le job de la page prime ; sinon un run externe est annonce."""
    base = {"running": True, "mode": "daily", "platforms": ["grok"], "done": []}
    assert runstate.external_scrape_state(base)["source"] == "job"
    assert runstate.external_scrape_state(base)["running"] is True

    monkeypatch.setattr(runstate, "read_run_state", lambda: {
        "pid": 4242, "mode": "monthly", "services": ["claude"],
        "started_at": "2026-11-01T12:00:00Z",
    })
    state = runstate.external_scrape_state({"running": False, "done": [{"platform": "grok"}]})
    assert state["running"] is True
    assert state["source"] == "cli"
    assert state["mode"] == "monthly"
    assert state["platforms"] == ["claude"]
    assert state["done"] == []  # pas de bilan d'un run precedent

    monkeypatch.setattr(runstate, "read_run_state", lambda: None)
    assert runstate.external_scrape_state({"running": False})["running"] is False


def test_refuse_ce_qui_n_est_pas_un_small_fichier(tmp_path):
    """Un processus local peut pre-creer `.state/aicv-run.json` : on refuse
    lien symbolique, FIFO et fichier enorme (deni de service / lecture)."""
    import json
    import os

    valid = json.dumps({"pid": os.getpid(), "mode": "daily",
                        "services": [], "started_at": "2026-11-01T12:00:00Z"})

    lien = tmp_path / "lien.json"
    cible = tmp_path / "cible.json"
    cible.write_text(valid, encoding="utf-8")
    lien.symlink_to(cible)
    assert runstate.read_run_state(path=lien) is None

    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    assert runstate.read_run_state(path=fifo) is None

    gros = tmp_path / "gros.json"
    gros.write_text('{"pad": "' + "x" * 5000 + '"}', encoding="utf-8")
    assert runstate.read_run_state(path=gros) is None

    # un lien dur n'a pas a etre accepte non plus
    dur = tmp_path / "dur.json"
    dur.write_text(valid, encoding="utf-8")
    dur_lien = tmp_path / "dur-lien.json"
    os.link(dur, dur_lien)
    assert runstate.read_run_state(path=dur_lien) is None
