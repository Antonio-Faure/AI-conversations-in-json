#!/usr/bin/env python3
"""Mini benchmark / moniteur de similarite des API internes.

Quatre commandes :

  - `shape`      : compare la forme d'un payload (payload d'etalon par defaut,
                   `--from <raw.json>` ou `--live`) a la reference versionnee
                   `tests/fixtures/api_shapes/<platform>.shape.json` ; affiche
                   le diff + le score et sort 1 si BREAK.
  - `update-ref` : regenere la reference (union des payloads d'etalon par
                   defaut, `--all` = corpus complet de la plateforme,
                   `--from <raw.json>` = payload unique) ; mise a jour manuelle
                   et assumee. Une reference mono-payload est exacte mais trop
                   etroite pour le monitoring : `--all` capture le vocabulaire
                   de formes observe (c'est le mode des fixtures versionnees).
  - `bench`      : benchmark live (etalon + N conversations via le client API
                   existant) : latence, octets, messages mappes, similarite de
                   forme, retries/429, echecs ; ecrit `bench/<date>.json`.
  - `report`     : agrege le dernier `exports/<platform>/api_health.json`.

Exemples:
    .venv/bin/python scripts/api_bench.py shape --platform chatgpt
    .venv/bin/python scripts/api_bench.py shape --platform grok --live --json
    .venv/bin/python scripts/api_bench.py update-ref --platform claude
    .venv/bin/python scripts/api_bench.py bench --platform chatgpt --n 3
    .venv/bin/python scripts/api_bench.py report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import (  # noqa: E402
    default_browser_factory,
    load_config,
    resolve_engine,
)
from src.schema import ConversationRef  # noqa: E402
from src.services import ACTIVE_SERVICES, SERVICE_CLASSES  # noqa: E402
from src.services.base import NoopSession  # noqa: E402
from src.utils.api_shape import (  # noqa: E402
    compare_shapes,
    flatten_shape,
    level_for,
    merge_shapes,
    shape_of,
    signature_of,
    signature_of_shape,
)
from src.utils.etalon import etalons_file, load_etalons  # noqa: E402
from src.utils.file_utils import (  # noqa: E402
    conversation_list_path,
    load_conversation_list,
    now_iso_z,
    platform_dir,
    read_json,
    write_json_atomic,
)
from src.utils.logging import get_logger, setup_logging  # noqa: E402
from src.utils.raw_payload import raw_payload_dir, raw_payload_path  # noqa: E402

log = get_logger("api_bench")

#: dossier des references de forme versionnees
SHAPES_DIR = ROOT / "tests" / "fixtures" / "api_shapes"
#: dossier des rapports de bench (gitignore)
BENCH_DIR = ROOT / "bench"
#: nombre de chemins affiches par categorie de diff (la sortie --json est complete)
DIFF_DISPLAY_LIMIT = 15


def shape_ref_path(platform: str) -> Path:
    """Chemin de la reference de forme d'une plateforme."""
    return SHAPES_DIR / f"{platform}.shape.json"


def load_reference(platform: str) -> Optional[Dict[str, Any]]:
    """Document de reference d'une plateforme (None si absent/invalide)."""
    doc = read_json(shape_ref_path(platform), default=None)
    if isinstance(doc, dict) and isinstance(doc.get("shape"), dict):
        return doc
    return None


class _RetryCounter(logging.Handler):
    """Compte les 429 journalises par les clients API (`HTTP 429, nouvel essai`)."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.retries = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if "429" in record.getMessage():
                self.retries += 1
        except Exception:  # noqa: BLE001 (jamais casser un log)
            pass


# -- sources de payload --------------------------------------------------------


def _etalon_entries(args: argparse.Namespace, platform: str) -> List[Dict[str, Any]]:
    entries = load_etalons(etalons_file(getattr(args, "etalons", None)))
    return list((entries or {}).get(platform) or [])


def _etalon_entry(args: argparse.Namespace, platform: str) -> Optional[Dict[str, Any]]:
    """Premiere conversation d'etalon d'une plateforme (la principale)."""
    entries = _etalon_entries(args, platform)
    return entries[0] if entries else None


def _find_raw_payload(
    output_dir: Path, platform: str, conv_id: str
) -> Optional[Path]:
    """Payload brut d'une conversation (inventaire, puis scan de `raw/`)."""
    index = load_conversation_list(conversation_list_path(output_dir, platform))
    stem = (index.get(conv_id) or {}).get("file")
    if stem:
        path = raw_payload_path(output_dir, platform, stem)
        if path.exists():
            return path
    for path in sorted(raw_payload_dir(output_dir, platform).glob("*.json")):
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            continue
        candidates = (
            data.get("conversation_id"),
            (data.get("conversation") or {}).get("conversationId"),
            (data.get("chat") or {}).get("id"),
        )
        if conv_id in {str(c) for c in candidates if c}:
            return path
    return None


def _payload_from_etalon(
    args: argparse.Namespace, config: Dict[str, Any], platform: str
) -> Tuple[Optional[Any], Optional[str], Optional[Dict[str, Any]]]:
    """(payload, source, etalon) depuis le payload brut de l'etalon principal."""
    etalon = _etalon_entry(args, platform)
    if not etalon or not etalon.get("id"):
        print(
            f"{platform}: aucun etalon configure (scripts/etalons.json)",
            file=sys.stderr,
        )
        return None, None, None
    path = _find_raw_payload(
        Path(config["output_dir"]), platform, str(etalon["id"])
    )
    if path is None:
        print(
            f"{platform}: payload brut de l'etalon introuvable "
            f"({etalon['id']}) -> lancer un scrape ou --from",
            file=sys.stderr,
        )
        return None, None, etalon
    return read_json(path, default=None), str(path), etalon


def _etalon_payload_paths(
    args: argparse.Namespace, config: Dict[str, Any], platform: str
) -> List[Path]:
    """Payloads bruts de toutes les conversations d'etalon de la plateforme."""
    paths: List[Path] = []
    for entry in _etalon_entries(args, platform):
        cid = str(entry.get("id") or "")
        if not cid:
            continue
        path = _find_raw_payload(Path(config["output_dir"]), platform, cid)
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def _corpus_payload_paths(config: Dict[str, Any], platform: str) -> List[Path]:
    """Tous les payloads bruts disponibles pour la plateforme."""
    return sorted(raw_payload_dir(Path(config["output_dir"]), platform).glob("*.json"))


def _reference_document(
    platform: str,
    payloads: List[Any],
    scope: str,
    etalon: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Document de reference : union des formes des payloads fournis."""
    shapes = [shape_of(payload) for payload in payloads]
    shape = merge_shapes(shapes)
    if shape is None:
        return None
    return {
        "platform": platform,
        "scope": scope,
        "etalon": etalon,
        "sources": len(shapes),
        "signature": signature_of_shape(shape),
        "shape": shape,
    }


def _resolve_source(
    args: argparse.Namespace, config: Dict[str, Any], platform: str
) -> Tuple[Optional[Any], Optional[str]]:
    """Payload demande : `--from <raw.json>`, sinon payload brut d'etalon."""
    if getattr(args, "from_path", None):
        path = Path(args.from_path)
        payload = read_json(path, default=None)
        if payload is None:
            print(f"payload illisible: {path}", file=sys.stderr)
            return None, None
        return payload, str(path)
    payload, source, _etalon = _payload_from_etalon(args, config, platform)
    return payload, source


# -- service API (client existant) --------------------------------------------


def _build_service(platform: str, config: Dict[str, Any]) -> Tuple[Any, Any]:
    """Session + service API de la plateforme (meme fabrique que l'orchestrateur)."""
    cls = SERVICE_CLASSES[platform]
    engine = resolve_engine(platform, config)
    attempt = "playwright" if engine == "auto" else engine
    svc_config = (config.get("services") or {}).get(platform) or {}
    if getattr(cls, "uses_browser", True):
        session = default_browser_factory(
            Path(config["profile_dir"]), platform, dict(config, _engine=attempt)
        )
    else:
        # service API HTTP + cookies : aucun navigateur a ouvrir
        session = NoopSession()
    service = cls(session, config)
    if svc_config.get("url"):
        service.home_url = svc_config["url"]
    return session, service


def _close(session: Any) -> None:
    try:
        session.close()
    except Exception:  # noqa: BLE001
        pass


def _ref_from_etalon(platform: str, etalon: Dict[str, Any]) -> ConversationRef:
    return ConversationRef(
        service=platform,
        id=str(etalon.get("id") or ""),
        url=str(etalon.get("url") or ""),
        title=etalon.get("label") or None,
    )


def _fetch_live(
    config: Dict[str, Any], platform: str, ref: ConversationRef
) -> Tuple[Optional[Any], Any]:
    """Fetch live d'une conversation via le client API du service."""
    session, service = _build_service(platform, config)
    try:
        conv, _html = service.export_conversation_with_html(ref)
        payload = service.take_raw_payload()
    finally:
        _close(session)
    return payload, conv


# -- affichage -----------------------------------------------------------------


def _short_signature(signature: Optional[str]) -> str:
    return (signature or "?")[:16]


def _print_diff(report: Dict[str, Any]) -> None:
    sections = (
        ("ajoutes", report["added"]),
        ("supprimes", report["removed"]),
        ("types changes", report["type_changes"]),
        ("cardinalite", report["cardinality_changes"]),
        ("formats", report["format_changes"]),
    )
    empty = True
    for label, paths in sections:
        if not paths:
            continue
        empty = False
        shown = ", ".join(paths[:DIFF_DISPLAY_LIMIT])
        more = (
            f" (+{len(paths) - DIFF_DISPLAY_LIMIT} autres)"
            if len(paths) > DIFF_DISPLAY_LIMIT
            else ""
        )
        print(f"  {label} ({len(paths)}): {shown}{more}")
    if empty:
        print("  aucun ecart de forme")


def _print_shape_report(
    platform: str, report: Dict[str, Any], ref_doc: Dict[str, Any], source: str
) -> None:
    print(
        f"{platform}: score={report['score']:.1f}% {report['level']} "
        f"(structure={report['structure']:.1f} types={report['types']:.1f} "
        f"cardinalite={report['cardinality']:.1f})"
    )
    print(
        f"  source: {source} "
        f"(reference {ref_doc.get('scope') or '?'}/"
        f"{ref_doc.get('sources') or '?'} payloads, "
        f"signature ref={_short_signature(ref_doc.get('signature'))} "
        f"live={_short_signature(report.get('signature_live'))})"
    )
    _print_diff(report)


def _print_bench_table(report: Dict[str, Any], out_path: Path) -> None:
    summary = report["summary"]
    print(
        f"{report['platform']} bench (reference "
        f"{_short_signature((report.get('reference') or {}).get('signature'))}, "
        f"n={summary['n']})"
    )
    for entry in report["results"]:
        if entry.get("error"):
            print(
                f"  [ECHEC ] {entry['kind']:<6} {entry['conversation_id'][:12]:<12} "
                f"latence={entry['latency_s']:.2f}s retry={entry['retries_429']} "
                f"erreur={entry['error'][:80]}"
            )
            continue
        print(
            f"  [{entry['level']:<5}] {entry['kind']:<6} "
            f"{entry['conversation_id'][:12]:<12} latence={entry['latency_s']:.2f}s "
            f"octets={entry['bytes']:<8} messages={entry['messages']:<4} "
            f"score={entry['score']:.1f}%"
        )
    print(
        f"  total: {summary['fetched']} fetch, {summary['failed']} echec(s), "
        f"{summary['retries_429']} retry/429, "
        f"score min={summary['min_score']} moyen={summary['mean_score']}, "
        f"latence moyenne={summary['mean_latency_s']}s"
    )
    print(f"  ecrit: {out_path}")


def _print_report_table(rows: Dict[str, Optional[Dict[str, Any]]]) -> None:
    header = f"{'plateforme':<12} {'n':>5} {'moyen':>7} {'min':>7} {'drift':>6} {'break':>6}  maj"
    print(header)
    for platform, health in rows.items():
        if not health:
            print(f"{platform:<12} {'-':>5} {'-':>7} {'-':>7} {'-':>6} {'-':>6}  (aucun rapport)")
            continue
        print(
            f"{platform:<12} {health.get('n', 0):>5} "
            f"{str(health.get('mean_score')):>7} {str(health.get('min_score')):>7} "
            f"{health.get('drifts', 0):>6} {health.get('breaks', 0):>6}  "
            f"{health.get('updated_at') or '?'}"
        )


# -- commandes -----------------------------------------------------------------


def cmd_shape(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    platform = args.platform
    ref_doc = load_reference(platform)
    if ref_doc is None:
        print(
            f"reference de forme absente: {shape_ref_path(platform)} "
            f"(lancer update-ref --platform {platform})",
            file=sys.stderr,
        )
        return 2
    if args.live:
        etalon = _etalon_entry(args, platform)
        if not etalon or not etalon.get("id"):
            print(f"{platform}: aucun etalon configure", file=sys.stderr)
            return 2
        ref = _ref_from_etalon(platform, etalon)
        payload, _conv = _fetch_live(config, platform, ref)
        source = f"live:{ref.id}"
    else:
        payload, source = _resolve_source(args, config, platform)
    if payload is None:
        return 2
    report = compare_shapes(ref_doc["shape"], shape_of(payload))
    report["platform"] = platform
    report["source"] = source
    report["signature_ref"] = ref_doc.get("signature")
    report["signature_live"] = signature_of(payload)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_shape_report(platform, report, ref_doc, source or "?")
    return 1 if report["level"] == "BREAK" else 0


def cmd_update_ref(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    platform = args.platform
    etalon_label: Optional[str] = None
    source = ""
    if getattr(args, "from_path", None):
        path = Path(args.from_path)
        payload = read_json(path, default=None)
        if payload is None:
            print(f"payload illisible: {path}", file=sys.stderr)
            return 2
        payloads = [payload]
        scope = "from"
        source = str(path)
    elif getattr(args, "all", False):
        raw_dir = raw_payload_dir(Path(config["output_dir"]), platform)
        paths = _corpus_payload_paths(config, platform)
        if not paths:
            print(
                f"{platform}: aucun payload brut dans {_display_path(str(raw_dir))}",
                file=sys.stderr,
            )
            return 2
        payloads = [read_json(path, default=None) for path in paths]
        scope = "corpus"
        source = f"{len(paths)} payloads de {_display_path(str(raw_dir))}"
    else:
        paths = _etalon_payload_paths(args, config, platform)
        if not paths:
            print(
                f"{platform}: payloads bruts d'etalon introuvables "
                f"-> lancer un scrape, --all ou --from",
                file=sys.stderr,
            )
            return 2
        payloads = [read_json(path, default=None) for path in paths]
        scope = "etalons"
        etalon_label = next(
            (
                entry.get("label")
                for entry in _etalon_entries(args, platform)
                if entry.get("label")
            ),
            None,
        )
        source = ", ".join(_display_path(str(path)) for path in paths)
    doc = _reference_document(platform, payloads, scope, etalon_label)
    if doc is None:
        print(f"{platform}: payloads illisibles", file=sys.stderr)
        return 2
    out_path = shape_ref_path(platform)
    write_json_atomic(out_path, doc)
    paths_count = len(_flatten_paths(doc["shape"]))
    print(
        f"{platform}: reference ecrite {_display_path(str(out_path))} "
        f"(signature={_short_signature(doc['signature'])}, {paths_count} chemins, "
        f"scope={doc['scope']}, sources={doc['sources']})"
    )
    print(
        f"  source: {source}"
        + (f" (etalon {etalon_label})" if etalon_label else "")
    )
    return 0


def _bench_one(
    service: Any,
    kind: str,
    ref: ConversationRef,
    ref_shape: Dict[str, Any],
    counter: _RetryCounter,
) -> Dict[str, Any]:
    """Un fetch mesure : latence, octets, messages, similarite, retries."""
    entry: Dict[str, Any] = {
        "kind": kind,
        "conversation_id": ref.id,
        "title": ref.title or "",
        "url": ref.url or "",
    }
    before = counter.retries
    start = time.perf_counter()
    payload = None
    conv = None
    error: Optional[str] = None
    try:
        conv, _html = service.export_conversation_with_html(ref)
        payload = service.take_raw_payload()
    except Exception as exc:  # noqa: BLE001 (le bench continue sur les autres)
        error = f"{type(exc).__name__}: {exc}"[:300]
    entry["latency_s"] = round(time.perf_counter() - start, 3)
    entry["retries_429"] = counter.retries - before
    if error:
        entry["error"] = error
        entry["score"] = None
        entry["level"] = None
        return entry
    if payload is None:
        entry["error"] = "payload API brut absent (service en mode DOM ?)"
        entry["score"] = None
        entry["level"] = None
        return entry
    report = compare_shapes(ref_shape, shape_of(payload))
    entry["bytes"] = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    entry["messages"] = len(conv.messages) if conv is not None else None
    entry["score"] = report["score"]
    entry["level"] = report["level"]
    entry["signature"] = signature_of(payload)
    if report["level"] != "OK":
        entry["diff"] = {
            key: report[key]
            for key in (
                "added",
                "removed",
                "type_changes",
                "cardinality_changes",
                "format_changes",
            )
        }
    return entry


def _bench_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    fetched = [entry for entry in results if not entry.get("error")]
    scores = [entry["score"] for entry in fetched if entry.get("score") is not None]
    latencies = [entry["latency_s"] for entry in results]
    return {
        "n": len(results),
        "fetched": len(fetched),
        "failed": len(results) - len(fetched),
        "retries_429": sum(int(entry.get("retries_429") or 0) for entry in results),
        "bytes": sum(int(entry.get("bytes") or 0) for entry in fetched),
        "messages": sum(int(entry.get("messages") or 0) for entry in fetched),
        "mean_latency_s": (
            round(sum(latencies) / len(latencies), 3) if latencies else None
        ),
        "min_latency_s": min(latencies) if latencies else None,
        "max_latency_s": max(latencies) if latencies else None,
        "mean_score": (
            round(sum(scores) / len(scores), 2) if scores else None
        ),
        "min_score": min(scores) if scores else None,
        "level": (
            level_for(min(scores))
            if scores
            else ("BREAK" if results else "OK")
        ),
    }


def cmd_bench(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    platform = args.platform
    ref_doc = load_reference(platform)
    if ref_doc is None:
        print(
            f"reference de forme absente: {shape_ref_path(platform)} "
            f"(lancer update-ref --platform {platform})",
            file=sys.stderr,
        )
        return 2
    etalon = _etalon_entry(args, platform)
    if not etalon or not etalon.get("id"):
        print(f"{platform}: aucun etalon configure", file=sys.stderr)
        return 2

    counter = _RetryCounter()
    aicv_logger = logging.getLogger("aicv")
    aicv_logger.addHandler(counter)
    session, service = _build_service(platform, config)
    results: List[Dict[str, Any]] = []
    try:
        etalon_ref = _ref_from_etalon(platform, etalon)
        targets: List[Tuple[str, ConversationRef]] = [("etalon", etalon_ref)]
        try:
            discovered = service.list_conversations(limit=int(args.n) + 1)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: decouverte impossible (%s)", platform, exc)
            discovered = []
        for ref in discovered:
            if len(targets) - 1 >= int(args.n):
                break
            if ref.id == etalon_ref.id:
                continue
            targets.append(("echantillon", ref))
        for kind, ref in targets:
            results.append(_bench_one(service, kind, ref, ref_doc["shape"], counter))
    finally:
        aicv_logger.removeHandler(counter)
        _close(session)

    report = {
        "platform": platform,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": now_iso_z(),
        "reference": {
            "path": _display_path(str(shape_ref_path(platform))),
            "signature": ref_doc.get("signature"),
        },
        "results": results,
        "summary": _bench_summary(results),
    }
    out_path = Path(args.out) if args.out else BENCH_DIR / f"{report['date']}.json"
    write_json_atomic(out_path, report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_bench_table(report, out_path)
    summary = report["summary"]
    if summary["failed"] or summary["level"] == "BREAK":
        return 1
    return 0


def cmd_report(args: argparse.Namespace, config: Dict[str, Any]) -> int:
    output_dir = Path(config["output_dir"])
    rows: Dict[str, Optional[Dict[str, Any]]] = {
        platform: read_json(
            platform_dir(output_dir, platform) / "api_health.json", default=None
        )
        for platform in ACTIVE_SERVICES
    }
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_report_table(rows)
    return 0


# -- helpers -------------------------------------------------------------------


def _display_path(path: str) -> str:
    """Chemin relatif au depot si possible (lisibilite des rapports)."""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except (ValueError, OSError):
        pass
    # exports hors depot (symlink local) : garder la partie portable
    parts = resolved.parts
    if "exports" in parts:
        return str(Path(*parts[parts.index("exports"):]))
    return str(path)


def _flatten_paths(shape: Dict[str, Any]) -> List[str]:
    return sorted(flatten_shape(shape))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api_bench.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config", "-c", type=Path, default=ROOT / "config.yaml",
        help="fichier de configuration YAML",
    )
    common.add_argument(
        "--exports", type=Path, default=None,
        help="surcharge output_dir de la config (dossier exports/)",
    )
    common.add_argument(
        "--etalons", type=Path, default=None,
        help="defaut: scripts/etalons.json (sinon l'exemple)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_shape = sub.add_parser(
        "shape", parents=[common],
        help="compare la forme d'un payload a la reference",
    )
    p_shape.add_argument("--platform", required=True, choices=sorted(ACTIVE_SERVICES))
    source = p_shape.add_mutually_exclusive_group()
    source.add_argument(
        "--from", dest="from_path", type=Path, metavar="RAW.JSON",
        help="payload a comparer (defaut: payload brut de l'etalon)",
    )
    source.add_argument(
        "--live", action="store_true",
        help="fetch live de l'etalon via le client API",
    )
    p_shape.add_argument("--json", action="store_true", help="rapport machine")

    p_update = sub.add_parser(
        "update-ref", parents=[common],
        help="regenere la reference (etalons par defaut, --all = corpus, --from = 1 payload)",
    )
    p_update.add_argument("--platform", required=True, choices=sorted(ACTIVE_SERVICES))
    source_group = p_update.add_mutually_exclusive_group()
    source_group.add_argument(
        "--from", dest="from_path", type=Path, metavar="RAW.JSON",
        help="payload source unique (reference exacte de ce payload)",
    )
    source_group.add_argument(
        "--all", action="store_true",
        help="union de TOUS les payloads bruts de la plateforme (vocabulaire complet)",
    )

    p_bench = sub.add_parser(
        "bench", parents=[common],
        help="benchmark live (etalon + N conversations)",
    )
    p_bench.add_argument("--platform", required=True, choices=sorted(ACTIVE_SERVICES))
    p_bench.add_argument(
        "--n", type=int, default=5, help="nombre d'echantillons en plus de l'etalon"
    )
    p_bench.add_argument("--json", action="store_true", help="rapport machine")
    p_bench.add_argument(
        "--out", type=Path, default=None, help="chemin du rapport (defaut: bench/<date>.json)"
    )

    p_report = sub.add_parser(
        "report", parents=[common],
        help="agrege les api_health.json des plateformes",
    )
    p_report.add_argument("--json", action="store_true", help="rapport machine")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    live = args.command == "bench" or getattr(args, "live", False)
    setup_logging(level=logging.INFO if live else logging.WARNING, console=True)
    config = load_config(args.config)
    if args.exports:
        config["output_dir"] = str(Path(args.exports).expanduser())
    if args.command == "shape":
        return cmd_shape(args, config)
    if args.command == "update-ref":
        return cmd_update_ref(args, config)
    if args.command == "bench":
        return cmd_bench(args, config)
    if args.command == "report":
        return cmd_report(args, config)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
