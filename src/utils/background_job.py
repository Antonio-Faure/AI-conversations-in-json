"""Job en tache de fond (scrape a la demande, controle de parseur).

Un seul job a la fois, sous le **verrou du cron** (`src/utils/runlock.py`) : un
job lance depuis la page web refuse de demarrer pendant un scrape (et
inversement). L'etat (en cours, journal, resultats par plateforme) est lu par
`snapshot()` — c'est ce que sert `GET /api/...` a la page web.

Les sous-classes fixent le `runner` appele par plateforme et les options du job.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional, Sequence

from .file_utils import now_iso_z
from .logging import ConsoleFormatter
from .runlock import RUN_LOCK_PATH, acquire_run_lock, release_run_lock

#: nombre de lignes de journal conservees dans l'etat
LOG_TAIL = 60


def make_orchestrator(config: Dict[str, Any]):
    """Orchestrateur (import paresseux : le module reste leger)."""
    from ..orchestrator import Orchestrator

    return Orchestrator(config)


class _JobLogHandler(logging.Handler):
    """Recopie les logs `aicv.*` dans le journal du job (page web)."""

    def __init__(self, sink: Callable[[str], None]):
        super().__init__(level=logging.INFO)
        self._sink = sink
        self.setFormatter(ConsoleFormatter(color=False))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(self.format(record))
        except Exception:  # noqa: BLE001 - jamais bloquant
            pass


class BackgroundJob:
    """Base des jobs : etat, verrou, journal, execution par plateforme.

    Le `runner` recoit `(config, platform, orchestrator=..., log=..., **options)`
    et rend un dictionnaire (le resultat de la plateforme) ou None.
    """

    #: options du job (nom -> defaut) : stockees dans l'etat, passees au runner
    options: Dict[str, Any] = {}
    #: libelle des messages de journal
    label = "job"
    #: runner par defaut (surcharge par les sous-classes)
    default_runner: Optional[Callable[..., Any]] = None

    def __init__(
        self,
        runner: Optional[Callable[..., Any]] = None,
        lock_path: str = RUN_LOCK_PATH,
        orchestrator_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ):
        self._runner = runner or self.default_runner
        if self._runner is None:
            raise ValueError("runner requis")
        self._lock_path = lock_path
        self._make_orchestrator = orchestrator_factory or make_orchestrator
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "platforms": [],
            "done": [],
            "error": None,
            "log": [],
            **self.options,
        }

    # -- lecture ---------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Etat courant (les dernieres lignes de journal)."""
        with self._lock:
            state = dict(self._state)
            state["log"] = list(self._state["log"])[-LOG_TAIL:]
            state["done"] = list(self._state["done"])
            return state

    # -- demarrage -------------------------------------------------------------

    def start(
        self, config: Dict[str, Any], platforms: Sequence[str], **options: Any
    ) -> Dict[str, Any]:
        """Lance le job ; leve RuntimeError si un job tourne deja."""
        with self._lock:
            if self._state["running"]:
                raise RuntimeError(f"un {self.label} est deja en cours")
            self._state.update({
                "running": True,
                "started_at": now_iso_z(),
                "finished_at": None,
                "platforms": list(platforms),
                "done": [],
                "error": None,
                "log": [],
                **{**self.options, **options},
            })
        self._thread = threading.Thread(
            target=self._run, args=(config, list(platforms), dict(options)),
            daemon=True,
        )
        self._thread.start()
        return self.snapshot()

    # -- execution -------------------------------------------------------------

    def _log(self, message: Any) -> None:
        with self._lock:
            self._state["log"].append(str(message))

    def format_done(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Entree de resultat stockee dans l'etat (surchargeable)."""
        return dict(entry)

    def _run(self, config, platforms, options) -> None:
        lock = acquire_run_lock(self._lock_path)
        logger = logging.getLogger("aicv")
        handler = _JobLogHandler(self._log)
        try:
            if lock is None:
                self._log("un scrape est en cours (verrou occupe) : job annule")
                with self._lock:
                    self._state["error"] = "scrape en cours"
                return
            logger.addHandler(handler)
            orchestrator = self._make_orchestrator(config)
            for platform in platforms:
                self._log(f"{platform}:")
                entry = self._runner(
                    config, platform,
                    orchestrator=orchestrator, log=self._log, **options,
                )
                if entry:
                    with self._lock:
                        self._state["done"].append(self.format_done(entry))
        except Exception as exc:  # noqa: BLE001
            self._log(f"erreur: {type(exc).__name__}: {exc}")
            with self._lock:
                self._state["error"] = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            logger.removeHandler(handler)
            release_run_lock(lock)
            with self._lock:
                self._state["running"] = False
                self._state["finished_at"] = now_iso_z()
