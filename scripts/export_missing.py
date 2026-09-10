#!/usr/bin/env python3
"""Exporte uniquement les conversations absentes du disque.

Utile apres un throttling : au lieu de re-scroller toute la liste (chaque
passe fait autant de vues de pages et re-declenche la limite), on compare
les refs de la decouverte aux fichiers exports et on ne scrape que le manque.

Usage: .venv/bin/python scripts/export_missing.py perplexity [chatgpt ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import (  # noqa: E402
    default_browser_factory,
    default_registry,
    load_config,
    resolve_engine,
)
from src.schema import parse_iso  # noqa: E402
from src.services.base import EmptyConversationError  # noqa: E402
from src.utils.file_utils import export_path, now_iso_z, write_json_atomic  # noqa: E402


def missing_ids(output_dir: Path, service_name: str) -> set[str]:
    return {p.stem for p in output_dir.glob(f"*/{service_name}/*.json")}


def conv_date(conv) -> str:
    dt = parse_iso(conv.last_message_at or conv.started_at)
    return dt.strftime("%Y-%m-%d") if dt else None


def run_service(service_name: str) -> None:
    config = load_config()

    # chemins relatifs au depot
    for key in ("output_dir", "profile_dir", "state_file", "log_file"):
        value = config.get(key)
        if value:
            p = Path(value)
            config[key] = str(p if p.is_absolute() else ROOT / p)
    config["headless"] = True
    output_dir = Path(config["output_dir"])
    engine = resolve_engine(service_name, config)
    if engine == "auto":
        engine = "playwright"
    config = dict(config, _engine=engine)

    registry = default_registry()
    cls = registry[service_name]
    session = default_browser_factory(
        Path(config["profile_dir"]), service_name, config
    )
    service = cls(session, config)

    done = missing_ids(output_dir, service_name)
    print(f"[{service_name}] {len(done)} conversations deja sur disque")

    with session:
        refs = service.list_conversations(limit=None)
        missing = [r for r in refs if r.id not in done]
        print(f"[{service_name}] decouvertes: {len(refs)} | manquantes: {len(missing)}")
        # garde-fou throttling : si la 1re conversation redirige (rate limite),
        # on abandonne l'essai (la boucle appelante retentera plus tard)
        probe = missing[:1]
        if probe:
            try:
                service.export_conversation(probe[0])
            except EmptyConversationError:
                pass
            except Exception as exc:  # noqa: BLE001
                print(f"[{service_name}] throttle detecte sur la 1re conv ({exc}) -> essai abandonne")
                return
        exported = skipped = failed = 0
        for ref in missing:
            try:
                conv = service.export_conversation(ref)
                conv.exported_at = now_iso_z()
                date = conv_date(conv) or now_iso_z()[:10]
                path = export_path(output_dir, date, service_name, conv.conversation_id)
                write_json_atomic(path, conv.to_dict(validate=False))
                exported += 1
                print(f"  exportee {conv.conversation_id} -> {path.name} ({len(conv.messages)} msgs)")
            except EmptyConversationError:
                skipped += 1
                print(f"  ignoree  {ref.id} (vide/tache)")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ECHEC    {ref.id}: {exc}")
        print(
            f"[{service_name}] termine: {exported} exportees, "
            f"{skipped} ignorees, {failed} echecs"
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for name in sys.argv[1:]:
        run_service(name)
