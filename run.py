#!/usr/bin/env python3
"""Point d'entree CLI: exporter les conversations IA en JSON standardise.

Exemples:
    python run.py --all                          # tous les services actifs
    python run.py --service chatgpt              # un seul service
    python run.py --all --date 2026-09-09        # dossier de sortie exports/2026-09-09/
    python run.py --service claude --limit 20    # debug: 20 conversations max
    python run.py --all --force                  # ignorer l'etat incrementiel
    python run.py --login chatgpt                # connexion interactive (profil persistant)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import Orchestrator, load_config  # noqa: E402
from src.services import SERVICE_CLASSES  # noqa: E402
from src.utils.file_utils import validate_date_arg  # noqa: E402
from src.utils.logging import setup_logging  # noqa: E402

log = logging.getLogger("aicv")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Export quotidien des conversations IA (Playwright/Botasaurus) en JSON standardise.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--service",
        "-s",
        action="append",
        choices=sorted(SERVICE_CLASSES),
        metavar="NAME",
        help="service a exporter (option repetable)",
    )
    target.add_argument(
        "--all", "-a", action="store_true", help="tous les services de la config"
    )
    parser.add_argument(
        "--date",
        "-d",
        metavar="YYYY-MM-DD",
        help="filtrer sur une date de derniere conversation + dossier de sortie",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=ROOT / "config.yaml",
        type=Path,
        help="fichier de configuration YAML",
    )
    parser.add_argument("--output-dir", type=Path, help="surcharge config.output_dir")
    parser.add_argument("--limit", type=int, help="max de conversations par service (debug)")
    parser.add_argument("--force", action="store_true", help="re-exporter meme si inchange")
    parser.add_argument(
        "--headful", action="store_true", help="navigateur visible (debug / login manuel)"
    )
    parser.add_argument(
        "--headless", action="store_true", help="forcer headless (surcharge config)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="logs DEBUG sur console"
    )
    parser.add_argument(
        "--login",
        choices=sorted(SERVICE_CLASSES),
        metavar="SERVICE",
        help="Ouvre un navigateur visible pour connecter le profil persistant, puis quitte",
    )
    return parser


def _apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    if args.output_dir:
        config["output_dir"] = str(args.output_dir)
    if args.headless:
        config["headless"] = True
    elif args.headful:
        config["headless"] = False
    return config


def _resolve_paths(config: dict) -> dict:
    """Chemins relatifs = relatifs au depot, peu importe le cwd d'execution."""
    for key in ("output_dir", "profile_dir", "state_file", "log_file", "screenshot_dir"):
        value = config.get(key)
        if value:
            path = Path(value)
            config[key] = str(path if path.is_absolute() else ROOT / path)
    return config


def _login_session(service_name: str, config: dict):
    """Session de login : meme moteur que l'export.

    Les cookies anti-bot (cf_clearance) sont lies a l'User-Agent : se
    connecter via Playwright puis exporter via Botasaurus invaliderait la
    session. Le moteur du service est donc respecte ici aussi.
    """
    from src.orchestrator import default_browser_factory, resolve_engine

    engine = resolve_engine(service_name, config)
    if engine == "auto":
        engine = "playwright"
    # login = toujours visible, quel que soit headless de la config
    login_config = dict(config, _engine=engine, headless=False)
    return default_browser_factory(
        Path(config["profile_dir"]), service_name, login_config
    )


def cmd_login(service_name: str, config: dict) -> int:
    cls = SERVICE_CLASSES[service_name]
    home_url = getattr(cls, "home_url", "")
    svc_cfg = (config.get("services") or {}).get(service_name) or {}
    url = svc_cfg.get("url") or home_url
    profile_dir = Path(config["profile_dir"]) / service_name
    session = _login_session(service_name, config)
    try:
        session.interactive_login(service_name, url)
    finally:
        session.close()
    print(f"[{service_name}] profil persistant enregistre: {profile_dir}")
    print("Les prochains runs headless utiliseront cette session.")
    return 0


def _print_summary(summary, config: dict) -> None:
    print(f"\n=== Resume ({summary.date}) ===")
    for name, result in summary.services.items():
        if result.skipped:
            state = f"SKIP ({result.skip_reason})"
        else:
            state = "OK" if not result.failed else f"ECHECS: {', '.join(result.failed[:5])}"
        print(
            f"{name:<12} exportees={len(result.exported):<4} "
            f"inchangees={len(result.unchanged):<4} "
            f"filtrees={len(result.out_of_range):<4} "
            f"ignorees={len(result.skipped_items):<4} [{state}]"
        )
    print(f"Sortie: {Path(config['output_dir']) / summary.date}")


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = _resolve_paths(_apply_cli_overrides(load_config(args.config), args))

    setup_logging(
        level=logging.DEBUG if args.verbose else logging.INFO,
        log_file=Path(config["log_file"]) if config.get("log_file") else None,
        console=True,
    )

    if args.login:
        return cmd_login(args.login, config)

    if not args.all and not args.service:
        log.error("choisir --all ou au moins un --service (voir --help)")
        return 2

    if args.date:
        try:
            validate_date_arg(args.date)
        except ValueError as exc:
            log.error(str(exc))
            return 2

    orchestrator = Orchestrator(config)
    try:
        summary = orchestrator.run(
            services=args.service,
            date=args.date,
            all_services=args.all,
            limit=args.limit,
            force=args.force,
        )
    except KeyboardInterrupt:
        log.warning("interrompu")
        return 130
    except ValueError as exc:
        log.error(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        log.error(f"echec critique: {exc}", exc_info=True)
        return 1

    _print_summary(summary, config)
    return 1 if summary.has_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
