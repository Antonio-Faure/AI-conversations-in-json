#!/usr/bin/env python3
"""Sauvegarde la base RAG avant reindex (copie coherente + retention).

Copie la base SQLite (`rag_db` de la config) dans `<dossier de la base>/backups/`
via l'API `backup` de sqlite3, puis supprime les sauvegardes au-dela de `--keep`
(defaut 7). Chainee avant `rag_index.py` par le cron.

    .venv/bin/python scripts/backup_rag.py
    .venv/bin/python scripts/backup_rag.py --keep 14
    .venv/bin/python scripts/backup_rag.py --list
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.utils.backup import (  # noqa: E402
    BACKUP_DIR,
    KEEP,
    backup_database,
    backup_paths,
)
from src.utils.file_utils import resolve_cli_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml",
                        help="fichier de configuration (defaut: config.yaml)")
    parser.add_argument("--db", type=Path, help="base SQLite (defaut: config rag_db)")
    parser.add_argument("--keep", type=int, default=KEEP,
                        help=f"sauvegardes conservees (defaut: {KEEP})")
    parser.add_argument("--list", action="store_true", help="lister les sauvegardes")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = resolve_cli_path(args.db or config["rag_db"], ROOT)

    if args.list:
        items = backup_paths(db_path.parent / BACKUP_DIR, db_path.stem)
        if not items:
            print(f"aucune sauvegarde dans {db_path.parent / BACKUP_DIR}")
            return 0
        for path in items:
            stat = path.stat()
            when = datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
            print(f"  {when}  {stat.st_size / 1e6:6.1f} Mo  {path.name}")
        return 0
    if args.keep < 1:
        parser.error("--keep doit etre >= 1")

    target = backup_database(db_path, keep=args.keep)
    return 0 if target else 1


if __name__ == "__main__":
    raise SystemExit(main())
