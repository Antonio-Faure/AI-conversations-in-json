#!/usr/bin/env python3
"""Construit les conversations d'etalonnage compactees (offline).

Pour chaque bot, on recupere la suite de tests que le chatbot a generee
lui-meme (export JSON, canvas HTML Mistral, ou fichier Markdown exporte depuis
un canvas/document ChatGPT/Claude), on la tague par capacite
(calibration/manifest.json) et on garde le plus petit ensemble couvrant tout.

    .venv/bin/python scripts/calibration_build.py
    .venv/bin/python scripts/calibration_build.py --bot gemini --report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.calibration import (  # noqa: E402
    apply_supplements,
    build_payload,
    bootstraps_from_export,
    extract_tests,
    load_manifest,
    suite_from_path,
)

CAL_DIR = ROOT / "calibration"
MANIFEST_PATH = CAL_DIR / "manifest.json"
SUPPLEMENTS_PATH = CAL_DIR / "supplements.json"

#: suite generee par le chatbot, par bot (JSON = export, html = canvas Mistral,
#: md = fichier Markdown exporte depuis un canvas/document ChatGPT/Claude)
SOURCES: Dict[str, Dict[str, Any]] = {
    "chatgpt": {"kind": "md", "path": "calibration/sources/chatgpt.md"},
    "claude": {"kind": "md", "path": "calibration/sources/claude.md"},
    "gemini": {
        "kind": "json",
        "path": "exports/gemini/documentation-des-capacites-de-gemini.json",
    },
    "grok": {
        "kind": "json",
        "path": "exports/grok/documentation-capabilities-data-tools-memory.json",
    },
    "perplexity": {
        "kind": "json",
        "path": "exports/perplexity/dresse-une-documentation-complete-et-structuree-de-tout-ce-que-tu-peux-faire-dan.json",
    },
    "mistral-work": {
        "kind": "json",
        "path": "exports/mistral/capacites-et-fonctionnalites-detaillees.json",
        "platform": "mistral",
        "mode": "work",
    },
    "mistral-chat": {
        "kind": "html",
        "path": "exports/mistral/capacites-et-fonctionnalites.html",
        "platform": "mistral",
        "mode": "chat",
        "json": "exports/mistral/capacites-et-fonctionnalites.json",
    },
}

#: conversation de reference pour extraire les deux amorces
BOOTSTRAP_SOURCE = SOURCES["perplexity"]["path"]


def _conversation_ref(spec: Dict[str, Any]) -> Optional[str]:
    json_path = ROOT / (spec.get("json") or spec["path"])
    if json_path.suffix != ".json" or not json_path.exists():
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload.get("url") or payload.get("conversation_id")


def _load_supplements() -> Dict[str, Dict[str, Any]]:
    if not SUPPLEMENTS_PATH.exists():
        return {}
    data = json.loads(SUPPLEMENTS_PATH.read_text(encoding="utf-8"))
    return dict(data.get("supplements") or {})


def _build_bot(name: str, spec: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    suite = suite_from_path(spec["kind"], ROOT / spec["path"])
    tests = extract_tests(suite or "")
    platform = spec.get("platform") or name
    payload = build_payload(
        platform=platform,
        tests=tests,
        manifest=manifest,
        mode=spec.get("mode"),
        source_conversation=_conversation_ref(spec),
        generated_from=str(spec["path"]),
    )
    return apply_supplements(payload, _load_supplements())


def _missing_payload(name: str, manifest: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    all_ids = [c["id"] for c in manifest.get("capabilities") or []]
    return {
        "platform": name,
        "status": "source_absente",
        "generated_from": str(spec["path"]),
        "messages": [],
        "capabilities_covered": [],
        "capabilities_missing": all_ids,
        "note": (
            f"Deposer la suite generee par le bot dans {spec['path']} "
            "(export Markdown du canvas/document), puis relancer calibration_build.py."
        ),
    }


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_bootstraps(pending: list) -> Path:
    payload = json.loads((ROOT / BOOTSTRAP_SOURCE).read_text(encoding="utf-8"))
    boot = bootstraps_from_export(payload)
    boot["pending_bots"] = sorted(pending)
    boot["note"] = (
        "Deux amorces a envoyer une fois a chaque bot sans suite : documenter les "
        "capacites, puis generer la suite de tests."
    )
    path = CAL_DIR / "bootstraps.json"
    _write(path, boot)
    return path


def print_sheet(name: str) -> int:
    """Fiche d'envoi : messages a poster dans l'ordre, avec les pieces jointes."""
    path = CAL_DIR / f"{name}.json"
    if not path.exists():
        print(f"{name}: calibration absente ({path.name})", file=sys.stderr)
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages") or []
    if not messages:
        print(f"{name}: aucune suite (statut {payload.get('status')})", file=sys.stderr)
        return 2
    mode = f" [mode {payload['mode']}]" if payload.get("mode") else ""
    print(f"\n# {name}{mode} — {len(messages)} messages a poster (dans l'ordre)")
    for i, message in enumerate(messages, 1):
        joint = f"   [PJ: {', '.join(message['attachments'])}]" if message.get("attachments") else ""
        print(f"{i:2}. {message['text']}{joint}")
    if any(m.get("attachments") for m in messages):
        media_root = Path(os.environ.get("AICV_RUN_DIR") or "/home/odin/Documents/code/aicv-run")
        print(f"   medias: {media_root / name / 'attachments'} "
              f"(generer: scripts/make_media.py --bot {name})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", action="append", help="ne traiter que ce(s) bot(s)")
    parser.add_argument("--report", action="store_true", help="afficher la couverture")
    parser.add_argument("--print", dest="print_bot", action="append", metavar="BOT",
                        help="afficher la fiche d'envoi (messages a poster) d'un bot")
    args = parser.parse_args()

    if args.print_bot:
        return max((print_sheet(name) for name in args.print_bot), default=0)

    manifest = load_manifest(MANIFEST_PATH)
    names = args.bot or list(SOURCES)
    CAL_DIR.mkdir(parents=True, exist_ok=True)

    for name in names:
        spec = SOURCES.get(name)
        if spec is None:
            print(f"{name}: source inconnue", file=sys.stderr)
            continue
        exists = (ROOT / spec["path"]).exists()
        payload = (
            _build_bot(name, spec, manifest) if exists
            else _missing_payload(name, manifest, spec)
        )
        _write(CAL_DIR / f"{name}.json", payload)
        if args.report:
            if not exists:
                print(f"{name:14} source absente -> {spec['path']}")
                continue
            covered = len(payload["capabilities_covered"])
            total = covered + len(payload["capabilities_missing"])
            print(
                f"{name:14} pool={payload['test_pool']:<4} retenus={len(payload['messages']):<3} "
                f"couverture={covered}/{total} "
                f"manquants={','.join(payload['capabilities_missing']) or '-'}"
            )

    if not args.bot:
        pending = [n for n, s in SOURCES.items() if not (ROOT / s["path"]).exists()]
        boot_path = _write_bootstraps(pending)
        if args.report:
            print(f"bootstraps ecrits: {boot_path.relative_to(ROOT)} (en attente: {', '.join(pending) or '-'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
