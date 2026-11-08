"""Etat d'un scrape lance hors de la page web (cron ou CLI).

`run.py` publie `.state/aicv-run.json` (pid, mode, services, demarrage) tant
qu'un scrape tourne, et le supprime a la fin : la page web peut ainsi afficher
« daily en cours » meme quand le run vient du cron. Le job lance depuis la page
a son propre etat (`BackgroundJob`, en memoire) ; ce fichier ne couvre donc que
les processus externes.

Le fichier vit dans `.state/` (dossier inscriptible par le service, le meme que
le verrou de scrape) et n'est jamais confie a un autre utilisateur : le cron
tourne avec `umask 077`, le service sous le meme compte.
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .file_utils import now_iso_z

#: etat publie par `run.py` (cron / ligne de commande)
RUN_STATE_PATH = Path(__file__).resolve().parents[2] / ".state" / "aicv-run.json"
#: au-dela, l'information est consideree perimee (run tue sans nettoyage)
MAX_AGE_SECONDS = 24 * 3600
#: un fichier d'etat est minuscule : au-dela, il est ignore (pas un etat)
MAX_STATE_BYTES = 4096
#: modes acceptes (voir `Orchestrator.run`)
MODES = ("daily", "monthly")


def _pid_alive(pid: int) -> bool:
    """Le processus existe-t-il encore ? (meme utilisateur, sans /proc garanti)"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # existe mais appartient a un autre compte
        return True
    except (OSError, OverflowError):
        return False
    return True


def _looks_like_runner(pid: int) -> bool:
    """Garde-fou anti-reutilisation de pid : le processus est-il `run.py` ?

    `/proc` peut etre masque (service systemd) : dans ce cas on ne peut pas
    verifier et on garde la reponse fondee sur la date et le pid vivant.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True
    return b"run.py" in raw


def _parse_started(value: Any) -> Optional[float]:
    """Horodatage ISO -> epoch (None si illisible)."""
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def write_run_state(
    mode: str,
    services: Optional[Iterable[str]] = None,
    pid: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Publie l'etat d'un run qui demarre (jamais bloquant pour le scrape)."""
    target = Path(path or RUN_STATE_PATH)
    data = {
        "pid": int(pid or os.getpid()),
        "mode": mode if mode in MODES else "daily",
        "services": [str(name) for name in (services or [])],
        "started_at": now_iso_z(),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        # O_NOFOLLOW : jamais d'ecriture a travers un lien symbolique
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data))
        os.replace(temporary, target)
    except OSError:
        pass


def clear_run_state(pid: Optional[int] = None, path: Optional[Path] = None) -> None:
    """Supprime l'etat publie par ce processus (laisse celui d'un autre)."""
    target = Path(path or RUN_STATE_PATH)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if pid is not None and data.get("pid") != int(pid):
        return  # un autre run a repris le fichier : ne pas l'effacer
    try:
        target.unlink()
    except OSError:
        pass


def external_scrape_state(base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Etat de scrape a afficher : job local prioritaire, sinon run externe.

    `base` est l'instantane du job en memoire (`BackgroundJob.snapshot()`).
    Un run externe n'expose ni progression par plateforme ni journal : les
    champs correspondants sont remis a zero et `source` vaut `cli`.
    """
    state = dict(base or {})
    state["source"] = "job"
    if state.get("running"):
        return state
    external = read_run_state()
    if external is None:
        return state
    state.update({
        "running": True,
        "source": "cli",
        "mode": external["mode"],
        "platforms": external["services"],
        "started_at": external["started_at"],
        "finished_at": None,
        "done": [],
        "error": None,
        "log": [],
    })
    return state


def _read_state_file(target: Path) -> Optional[Dict[str, Any]]:
    """Contenu de l'etat, en refusant tout ce qui n'est pas un petit fichier.

    Un processus local peut pre-creer `.state/aicv-run.json` : FIFO (bloque un
    worker du site dans `open()`), lien symbolique vers un fichier arbitraire,
    lien dur, ou fichier enorme. On n'accepte qu'un fichier regulier, unique,
    de taille bornee, ouvert sans suivre de lien.
    """
    try:
        info = target.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    if info.st_size <= 0 or info.st_size > MAX_STATE_BYTES:
        return None
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            return None
        raw = os.read(fd, MAX_STATE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_STATE_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_run_state(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Etat d'un run externe encore vivant, ou None (absent, perime, termine)."""
    data = _read_state_file(Path(path or RUN_STATE_PATH))
    if data is None:
        return None
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return None
    if pid <= 0 or not _pid_alive(pid):
        return None
    started = _parse_started(data.get("started_at"))
    if started is None:
        return None
    if abs(time.time() - started) > MAX_AGE_SECONDS:
        return None
    if not _looks_like_runner(pid):
        return None
    services = data.get("services")
    mode = str(data.get("mode") or "daily")
    return {
        "pid": pid,
        "mode": mode if mode in MODES else "daily",
        "services": [
            str(name) for name in (services if isinstance(services, list) else [])
        ],
        "started_at": data.get("started_at"),
    }
