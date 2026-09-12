#!/usr/bin/env python3
"""Point d'entree CLI : export des conversations IA en JSON + HTML.

ChatGPT et Claude. Sortie : exports/<platform>/<nom>.{json,html} plus
exports/<platform>/conversation_list.json (inventaire).

Modes:
    --monthly (ou --full) : liste TOUTES les conversations et les scrape toutes
    --daily               : scrape les inconnues + les 20 plus recentes

Exemples:
    python run.py --daily                 # routine quotidienne
    python run.py --monthly               # rafraichissement complet
    python run.py --daily --headful -v    # debug visible
    python run.py --login claude          # connexion interactive (profil persistant)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cookies import COOKIE_SERVICES, capture_cookies  # noqa: E402
from src.orchestrator import Orchestrator, load_config  # noqa: E402
from src.services import ACTIVE_SERVICES, SERVICE_CLASSES  # noqa: E402
from src.utils.logging import setup_logging  # noqa: E402

LOGIN_CHOICES = sorted(set(ACTIVE_SERVICES) | set(COOKIE_SERVICES))

log = logging.getLogger("aicv")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Export des conversations IA (ChatGPT, Claude, Gemini, Perplexity) en JSON standardise + HTML.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--monthly",
        "--full",
        dest="monthly",
        action="store_true",
        help="lister TOUTES les conversations et tout scraper",
    )
    mode.add_argument(
        "--daily",
        dest="daily",
        action="store_true",
        help="scraper les conversations inconnues + les 20 plus recentes",
    )
    parser.add_argument(
        "--service", "-s", action="append", choices=sorted(ACTIVE_SERVICES),
        metavar="NAME",
        help="ne scraper que ce(s) service(s) (option repetable ; defaut: tous les actifs)",
    )
    parser.add_argument("--config", "-c", default=ROOT / "config.yaml", type=Path,
                        help="fichier de configuration YAML")
    parser.add_argument("--limit", type=int, help="max de conversations (debug)")
    parser.add_argument(
        "--match", metavar="TEXTE",
        help="ne scraper que les conversations dont le titre (ou l'id) contient "
             "ce texte, insensible a la casse (ex: --match 'Conversation etalon')",
    )
    parser.add_argument(
        "--parallel", type=int, metavar="N",
        help="scraper N services (domaines differents) en parallele (defaut: config)",
    )
    parser.add_argument("--headful", action="store_true",
                        help="navigateur visible (debug / login manuel)")
    parser.add_argument("--headless", action="store_true",
                        help="forcer headless (surcharge config)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="logs DEBUG sur console")
    parser.add_argument(
        "--login",
        choices=LOGIN_CHOICES,
        metavar="SERVICE",
        help="ouvre un navigateur visible : profil du service (4 scrapers) ou "
             "capture des cookies (grok, mistral), puis quitte",
    )
    return parser


def _apply_cli_overrides(config: dict, args: argparse.Namespace) -> dict:
    if args.headless:
        config["headless"] = True
    elif args.headful:
        config["headless"] = False
    return config


def _resolve_paths(config: dict) -> dict:
    """Chemins relatifs = relatifs au depot, peu importe le cwd d'execution."""
    for key in ("output_dir", "profile_dir", "cookies_dir"):
        value = config.get(key)
        if value:
            path = Path(value)
            config[key] = str(path if path.is_absolute() else ROOT / path)
    return config


def _login_session(service_name: str, config: dict):
    """Session de login : meme moteur que l'export (cookies lies a l'UA)."""
    from src.orchestrator import default_browser_factory, resolve_engine

    engine = resolve_engine(service_name, config)
    if engine == "auto":
        engine = "playwright"
    login_config = dict(config, _engine=engine, headless=False)
    return default_browser_factory(Path(config["profile_dir"]), service_name, login_config)


def cmd_login(service_name: str, config: dict) -> int:
    cls = SERVICE_CLASSES[service_name]
    url = ((config.get("services") or {}).get(service_name) or {}).get("url") \
        or getattr(cls, "home_url", "")
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
    print(f"\n=== Resume (mode: {summary.mode}) ===")
    for name, result in summary.services.items():
        if result.skipped_reason:
            state = f"SKIP ({result.skipped_reason})"
        else:
            state = "OK" if not result.failed else f"ECHECS: {', '.join(result.failed[:5])}"
        print(
            f"{name:<12} decouvertes={result.discovered:<4} "
            f"cibles={result.targets:<4} ecrites={len(result.exported):<4} "
            f"patch={len(result.patched):<4} inchanges={len(result.unchanged):<4} "
            f"ignorees={len(result.skipped):<4} [{state}]"
        )
    print(f"Sortie: {Path(config['output_dir'])}")


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = _resolve_paths(_apply_cli_overrides(load_config(args.config), args))

    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO, console=True)

    if args.login:
        if args.login in COOKIE_SERVICES:
            return capture_cookies(args.login, ROOT / "cookies", config)
        return cmd_login(args.login, config)

    if not args.monthly and not args.daily:
        log.error("choisir --monthly (ou --full) ou --daily (voir --help)")
        return 2
    mode = "monthly" if args.monthly else "daily"

    orchestrator = Orchestrator(config)
    parallel = args.parallel if args.parallel is not None else int(config.get("parallel", 1))
    try:
        summary = orchestrator.run(
            mode=mode, limit=args.limit, services=args.service,
            parallel=parallel, match=args.match,
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
