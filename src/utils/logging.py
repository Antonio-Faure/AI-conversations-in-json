"""Logging structure: sortie console lisible + fichier JSON lines."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER_NAME = "aicv"

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"asctime", "message", "taskName"}


class ConsoleFormatter(logging.Formatter):
    """Format lisible pour terminal."""

    LEVEL_COLORS = {
        "DEBUG": "\033[2;37m",
        "INFO": "\033[0;36m",
        "WARNING": "\033[0;33m",
        "ERROR": "\033[0;31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool = True):
        super().__init__(datefmt="%H:%M:%S")
        self.color = color and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        level = record.levelname
        if self.color:
            level = f"{self.LEVEL_COLORS.get(level, '')}{level:<8}{self.RESET}"
        else:
            level = f"{level:<8}"
        base = f"{ts} {level} [{record.name}] {record.getMessage()}"
        payload = _extra_payload(record)
        if payload:
            base += " " + json.dumps(payload, ensure_ascii=False, default=str)
        if record.exc_info:
            base = base + "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """Une ligne JSON par record: ts, level, logger, msg + extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_extra_payload(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _extra_payload(record: logging.LogRecord) -> Dict[str, Any]:
    """Champs passes via `extra={...}` (tout ce qui n'est pas reserve)."""
    data = {}
    for key, value in record.__dict__.items():
        if key not in _RESERVED and not key.startswith("_"):
            data[key] = value
    return data


class JsonFileHandler(logging.FileHandler):
    """FileHandler qui cree les dossiers parents a la demande."""

    def __init__(self, filename: Path, encoding: str = "utf-8"):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(str(filename), encoding=encoding)


def setup_logging(
    level: int = logging.INFO,
    log_file: Optional[Path] = None,
    console: bool = True,
) -> logging.Logger:
    """Configure (ou reconfigure) le logger racine du projet.

    - console: formatter lisible sur stderr
    - log_file: formatter JSON lines pour exploitation machine

    Idempotent et appelable deux fois: un appel precedent sans log_file (ex:
    get_logger() au moment de l'import des modules) ne desactive pas le
    fichier JSON demande plus tard par la CLI.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    configured = getattr(logger, "_aicv_configured", False)

    if console and not _has_stream_handler(logger):
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(level)
        stream.setFormatter(ConsoleFormatter())
        logger.addHandler(stream)
    if configured:
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(level)

    want_file = str(Path(log_file)) if log_file else None
    current_file = next(
        (str(Path(h.baseFilename)) for h in logger.handlers if isinstance(h, JsonFileHandler)),
        None,
    )
    if want_file != current_file:
        for h in [h for h in logger.handlers if isinstance(h, JsonFileHandler)]:
            h.close()
            logger.removeHandler(h)
        if want_file:
            fh = JsonFileHandler(Path(want_file))
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(JsonFormatter())
            logger.addHandler(fh)

    logger._aicv_configured = True  # type: ignore[attr-defined]
    return logger


def _has_stream_handler(logger: logging.Logger) -> bool:
    return any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Sous-logger du namespace projet (ex: get_logger('services.chatgpt'))."""
    root = logging.getLogger(LOGGER_NAME)
    if not root.handlers:
        setup_logging()
    return root.getChild(name) if name else root


def log_fields(
    logger: logging.Logger,
    level: int,
    msg: str,
    extra: Optional[Dict[str, Any]] = None,
    **fields: Any,
) -> None:
    """Helper: log avec champs structures (visible console + JSON).

    Accepte `extra={...}` (style logging) et/ou kwargs; les cles reservees du
    LogRecord sont filtrees pour eviter un KeyError de la stdlib.
    """
    payload: Dict[str, Any] = dict(extra or {})
    payload.update(fields)
    safe = {k: v for k, v in payload.items() if k not in _RESERVED}
    logger.log(level, msg, extra=safe)
