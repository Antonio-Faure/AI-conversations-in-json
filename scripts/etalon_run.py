#!/usr/bin/env python3
"""Envoie la conversation d'etalonnage d'un bot (resumable, rate-limit aware).

Lit `queue.json` (messages a envoyer) et `state.json`, envoie les messages via
le driver de la plateforme, et ecrit `BILAN.md`. S'arrete proprement au
rate-limit et reprend au prochain lancement.

    .venv/bin/python scripts/etalon_run.py --bot gemini --dry-run
    .venv/bin/python scripts/etalon_run.py --bot gemini
    .venv/bin/python scripts/etalon_run.py --bot grok --url https://grok.com/chat/xxx
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.drivers import get_driver  # noqa: E402
from src.drivers.runner import EtalonRunner, load_queue, load_state  # noqa: E402
from src.orchestrator import (  # noqa: E402
    default_browser_factory,
    load_config,
    resolve_engine,
)

# dossier runtime hors depot (independant du worktree d'ou l'on execute)
RUN_ROOT = Path(os.environ.get("AICV_RUN_DIR") or "/home/odin/Documents/code/aicv-run")


def _resolve_paths(config: dict) -> dict:
    for key in ("output_dir", "profile_dir", "cookies_dir"):
        value = config.get(key)
        if value:
            path = Path(value)
            config[key] = str(path if path.is_absolute() else ROOT / path)
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--url", help="URL de la conversation cible (sinon nouvelle)")
    parser.add_argument("--engine", choices=("playwright", "botasaurus"),
                        help="moteur (defaut: botasaurus pour grok, sinon config)")
    parser.add_argument("--config", "-c", default=ROOT / "config.yaml", type=Path)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="ouvre la page, liste les champs trouves + screenshot, n'envoie rien")
    args = parser.parse_args()

    config = _resolve_paths(load_config(args.config))
    if args.headful:
        config["headless"] = False
    engine = args.engine or ("botasaurus" if args.bot == "grok" else resolve_engine(args.bot, config))
    config["_engine"] = "playwright" if engine == "auto" else engine

    run_dir = args.run_dir or (RUN_ROOT / args.bot)
    run_dir.mkdir(parents=True, exist_ok=True)
    driver_cls = get_driver(args.bot)

    session = default_browser_factory(Path(config["profile_dir"]), args.bot, config)
    try:
        driver = driver_cls(session, config)
        if args.dry_run:
            driver.open_home()
            session.wait_ms(2500)
            found = [s for s in driver.input_selectors if session.is_element_present(s)]
            shot = run_dir / "dry_run.png"
            session.screenshot(shot)
            print(f"input selectors presents: {found or 'aucun'}")
            print(f"screenshot: {shot}")
            return 0

        queue_path = args.queue or (run_dir / "queue.json")
        if not queue_path.exists():
            print(f"queue absente: {queue_path}", file=sys.stderr)
            return 2
        messages = load_queue(queue_path)
        state = load_state(run_dir / "state.json", args.bot, run_dir)
        if args.url:
            state.target_url = args.url
        status = EtalonRunner(driver, run_dir, messages, state).run()
        print(f"statut: {status}")
        print(f"bilan: {run_dir / 'BILAN.md'}")
        return 0 if status in ("done", "rate_limited") else 1
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
