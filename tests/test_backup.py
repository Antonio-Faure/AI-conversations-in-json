"""Tests de la sauvegarde locale des bases SQLite (src/utils/backup.py)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.utils.backup import (
    BACKUP_DIR,
    backup_database,
    backup_paths,
    prune_backups,
)


def _make_db(path, rows: int = 3):
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE t (n INTEGER)")
    connection.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(rows)])
    connection.commit()
    connection.close()
    return path


def test_backup_copie_la_base(tmp_path):
    db_path = _make_db(tmp_path / "rag.db", rows=5)
    target = backup_database(db_path, keep=7, log=lambda *_: None)
    assert target is not None
    assert target.parent == tmp_path / BACKUP_DIR
    assert target.name.startswith("rag-") and target.suffix == ".db"
    copy = sqlite3.connect(str(target))
    try:
        assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    finally:
        copy.close()


def test_backup_absente_rend_none(tmp_path):
    assert backup_database(tmp_path / "absente.db", log=lambda *_: None) is None


def test_backup_retention_garde_les_plus_recentes(tmp_path):
    db_path = _make_db(tmp_path / "rag.db")
    for minute in range(5):
        backup_database(
            db_path,
            keep=3,
            now=datetime(2026, 1, 1, 12, minute, tzinfo=timezone.utc),
            log=lambda *_: None,
        )
    items = backup_paths(tmp_path / BACKUP_DIR, "rag")
    assert len(items) == 3
    # les plus recentes d'abord (tri decroissant sur le nom horodate)
    assert [p.name for p in items] == sorted((p.name for p in items), reverse=True)


def test_prune_garde_toujours_la_derniere(tmp_path):
    backup_dir = tmp_path / BACKUP_DIR
    backup_dir.mkdir()
    for name in ("rag-20260101-000000.db", "rag-20260102-000000.db"):
        (backup_dir / name).write_bytes(b"x")
    # keep=1 : seule la plus recente reste
    assert prune_backups(backup_dir, "rag", keep=1) == 1
    assert [p.name for p in backup_paths(backup_dir, "rag")] == [
        "rag-20260102-000000.db"
    ]
    # keep=0 est ramene a 1 : la derniere n'est jamais supprimee
    assert prune_backups(backup_dir, "rag", keep=0) == 0
    assert len(backup_paths(backup_dir, "rag")) == 1
