#!/usr/bin/env python3
"""Controle de parseur : re-scrape un echantillon aleatoire et compare a l'archive.

Pour chaque plateforme, tire `--per-platform` conversations de taille moyenne ou
grande (`--min-messages`), les re-scrape **sans rien ecrire dans l'archive** et
compare leurs metriques (messages, alternance des roles, images, tableaux,
listes, langues de code, LaTeX) a celles du JSON deja exporte. Le bilan est
ecrit dans `exports/<platform>/recheck.json` et affiche par le tableau de bord
(marquage fort quand la regression est catastrophique).

Meme moteur que la page web « Test des parseurs » et l'outil MCP
(`src/utils/recheck.py`).

    .venv/bin/python scripts/recheck.py                          # 3 par plateforme
    .venv/bin/python scripts/recheck.py --per-platform 5 --min-messages 12
    .venv/bin/python scripts/recheck.py --platform gemini --seed 42
    .venv/bin/python scripts/recheck.py --report                 # bilan du dernier run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import (  # noqa: E402
    Orchestrator,
    load_config,
    resolve_config_paths,
)
from src.utils.file_utils import (  # noqa: E402
    RECHECK_NAME,
    resolve_cli_path,
)
from src.utils.recheck import (  # noqa: E402
    MIN_MESSAGES,
    PER_PLATFORM,
    run_platform_recheck,
)

VERDICT_LABEL = {
    "ok": "OK",
    "attention": "ATTENTION",
    "critique": "CRITIQUE",
    "inconnu": "?",
}


def print_report(output_dir: Path, platforms=None) -> int:
    """Affiche le dernier bilan connu par plateforme (lecture seule)."""
    names = platforms or sorted(
        path.name for path in output_dir.iterdir()
        if path.is_dir() and (path / RECHECK_NAME).is_file()
    )
    problems = 0
    for platform in names:
        path = output_dir / platform / RECHECK_NAME
        if not path.is_file():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        verdict = report.get("verdict") or "inconnu"
        if verdict in ("critique", "attention"):
            problems += 1
        retention = report.get("retention")
        retention_txt = f"{round(retention * 100)} %" if retention is not None else "?"
        print(
            f"  [{VERDICT_LABEL.get(verdict, verdict):8}] {platform:11} "
            f"{report.get('checked', 0)} conversation(s), "
            f"retention {retention_txt}, verifie {report.get('checked_at', '?')}"
        )
        for item in report.get("conversations") or []:
            if item.get("status") == "ok":
                continue
            reason = item.get("error") or ", ".join(item.get("lost") or []) or "messages"
            print(f"      - {item.get('title') or item.get('conversation_id')}: {reason}")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml",
                        help="fichier de configuration (defaut: config.yaml)")
    parser.add_argument("--output", "-o", type=Path, help="racine des exports")
    parser.add_argument("--platform", action="append",
                        help="limiter a une plateforme (repetable)")
    parser.add_argument("--per-platform", type=int, default=PER_PLATFORM,
                        help=f"conversations tirees par plateforme (defaut: {PER_PLATFORM})")
    parser.add_argument("--min-messages", type=int, default=MIN_MESSAGES,
                        help=f"taille minimale des conversations (defaut: {MIN_MESSAGES})")
    parser.add_argument("--seed", type=int, default=0,
                        help="graine du tirage aleatoire (reproductible)")
    parser.add_argument("--dry-run", action="store_true",
                        help="ne pas ecrire les rapports recheck.json")
    parser.add_argument("--report", action="store_true",
                        help="afficher le dernier bilan (aucun scrape)")
    args = parser.parse_args()

    config = resolve_config_paths(load_config(args.config))
    output_dir = resolve_cli_path(args.output or config["output_dir"], ROOT)

    if args.report:
        return print_report(output_dir, args.platform)

    if args.per_platform < 1:
        parser.error("--per-platform doit etre >= 1")
    orchestrator = Orchestrator(config)
    platforms = args.platform or orchestrator._service_names()
    unknown = [name for name in platforms if name not in orchestrator.registry]
    if unknown:
        parser.error(f"plateforme(s) inconnue(s): {', '.join(unknown)}")

    reports = []
    for platform in platforms:
        print(f"{platform}:")
        report = run_platform_recheck(
            config, platform,
            args.per_platform, args.min_messages, args.seed,
            orchestrator=orchestrator, dry_run=args.dry_run,
        )
        if report:
            reports.append(report)

    critical = [r for r in reports if r["verdict"] == "critique"]
    attention = [r for r in reports if r["verdict"] == "attention"]
    print()
    print("BILAN")
    for report in reports:
        retention = report.get("retention")
        retention_txt = f"{round(retention * 100)} %" if retention is not None else "?"
        ignored = f", {report['ignored']} ignoree(s)" if report.get("ignored") else ""
        print(
            f"  [{VERDICT_LABEL.get(report['verdict'], report['verdict']):8}] "
            f"{report['platform']:11} {report['checked']} verifiee(s) "
            f"({report['ok']} ok, {report['degraded']} degradee(s), "
            f"{report['failed']} echec(s){ignored}), retention {retention_txt}"
        )
    if critical:
        print(f"\n!! {len(critical)} plateforme(s) CRITIQUE(S) : "
              f"{', '.join(r['platform'] for r in critical)}", file=sys.stderr)
        return 2
    if attention:
        print(f"\n{len(attention)} plateforme(s) a surveiller : "
              f"{', '.join(r['platform'] for r in attention)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
