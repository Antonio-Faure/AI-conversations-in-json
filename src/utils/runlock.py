"""Verrou de scrape partage (cron, page web, outils).

Un seul scrape a la fois : le cron prend `.state/aicv-run.lock` (flock -n)
avant `run.py`, et la page web prend le meme verrou avant de lancer un scrape
ou un controle de parseur. Sans lui, deux processus partageraient les memes
profils navigateur (cookies lies a l'UA) et se marcheraient dessus.

Le verrou vit dans le depot (`.state/`, deja monte en ecriture dans le service)
et non dans `/tmp` : le service tourne avec `PrivateTmp=yes`, donc son `/tmp`
n'est pas celui du cron.
"""

from __future__ import annotations

import os
from pathlib import Path

#: chemin du verrou partage avec le cron (voir la crontab)
RUN_LOCK_PATH = str(
    Path(__file__).resolve().parents[2] / ".state" / "aicv-run.lock"
)


def acquire_run_lock(path: str = RUN_LOCK_PATH):
    """Verrou exclusif non bloquant ; rend le descripteur, ou None si occupe."""
    handle = None
    try:
        import fcntl

        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "w")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if handle is not None:
            handle.close()
        return None
    return handle


def release_run_lock(handle) -> None:
    """Relache le verrou (sans erreur s'il est deja libere ou absent)."""
    if handle is None:
        return
    try:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()
