"""Scrape a la demande (page web) : un service par plateforme, en tache de fond.

Meme verrou que le cron (`src/utils/runlock.py`) : un scrape lance depuis la page
refuse de demarrer pendant un autre scrape (et inversement). Le mode reprend
celui de `run.py` : `daily` (inconnues + 20 plus recentes) ou `monthly` (tout).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence

from .background_job import BackgroundJob

#: mode par defaut (voir `Orchestrator.run_service`)
DEFAULT_MODE = "daily"


def run_platform_scrape(
    config: Dict[str, Any],
    platform: str,
    mode: str = DEFAULT_MODE,
    limit: Optional[int] = None,
    orchestrator: Any = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Scrape une plateforme et rend son bilan (compteurs du run)."""
    from ..orchestrator import MODES, Orchestrator

    if mode not in MODES:
        raise ValueError(f"mode inconnu: {mode!r} (attendu: {', '.join(MODES)})")
    orchestrator = orchestrator or Orchestrator(config)
    log(f"{platform}: scrape {mode}")
    result = orchestrator.run_service(platform, mode, limit=limit)
    if result.skipped_reason:
        log(f"{platform}: ignore ({result.skipped_reason})")
    return {
        "platform": platform,
        "mode": mode,
        "discovered": result.discovered,
        "targets": result.targets,
        "exported": len(result.exported),
        "patched": len(result.patched),
        "failed": len(result.failed),
        "skipped": result.skipped_reason,
        "ok": result.ok,
    }


class ScrapeJob(BackgroundJob):
    """Scrape a la demande en tache de fond (un seul job a la fois)."""

    label = "scrape"
    options = {"mode": DEFAULT_MODE, "limit": None}
    default_runner = staticmethod(run_platform_scrape)

    def start(
        self,
        config: Dict[str, Any],
        platforms: Sequence[str],
        mode: str = DEFAULT_MODE,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        return super().start(config, platforms, mode=mode, limit=limit)
