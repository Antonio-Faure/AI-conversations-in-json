"""Sauvegarde locale d'une base SQLite (copie coherente + retention).

Utilise l'API `backup` de sqlite3 : la copie est coherente meme si la base est
en mode WAL ou en cours d'ecriture (le cron sauvegarde avant chaque reindex).
Les sauvegardes vivent dans un sous-dossier `backups/` a cote de la base.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

#: sous-dossier des sauvegardes, a cote de la base
BACKUP_DIR = "backups"
#: nombre de sauvegardes conservees par defaut
KEEP = 7


def backup_paths(backup_dir: Path | str, stem: str) -> List[Path]:
    """Sauvegardes existantes de cette base, les plus recentes d'abord."""
    return sorted(Path(backup_dir).glob(f"{stem}-*.db"), reverse=True)


def prune_backups(backup_dir: Path | str, stem: str, keep: int = KEEP) -> int:
    """Supprime les sauvegardes au-dela de `keep` (garde toujours la recente)."""
    removed = 0
    for path in backup_paths(backup_dir, stem)[max(1, keep):]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def backup_database(
    db_path: Path | str,
    keep: int = KEEP,
    backup_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    log=print,
) -> Optional[Path]:
    """Copie la base dans `backups/` et elague ; rend le chemin ecrit (ou None).

    `now` fixe l'horodatage du nom de fichier (tests) ; `backup_dir` surcharge
    le dossier de destination.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        log(f"base absente : {db_path}")
        return None
    target_dir = Path(backup_dir) if backup_dir else db_path.parent / BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{db_path.stem}-{stamp}.db"
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    removed = prune_backups(target_dir, db_path.stem, keep)
    size = target.stat().st_size / 1e6
    log(
        f"sauvegarde : {target.name} ({size:.1f} Mo), "
        f"{removed} ancienne(s) supprimee(s)"
    )
    return target
