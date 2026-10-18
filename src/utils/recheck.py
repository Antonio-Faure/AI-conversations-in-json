"""Controle de parseur : compare une conversation re-scrapee a sa version archivee.

Reutilise les metriques de `src/utils/etalon.py` (messages, alternance des roles,
images, tableaux, listes, langues de code, LaTeX). Le verdict par conversation et
par plateforme alimente le tableau de bord (marquage fort si catastrophique).

`run_platform_recheck` fait le travail (re-scrape + comparaison + rapport) et
`RecheckJob` l'execute en tache de fond (page web et serveur MCP).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .background_job import BackgroundJob
from .etalon import conversation_metrics, regressions
from .file_utils import (
    CONVERSATION_LIST_NAME,
    RECHECK_NAME,
    find_export_by_id,
    now_iso_z,
    write_json_atomic,
)

#: taille minimale (messages) pour qu'une conversation soit un bon canari
MIN_MESSAGES = 8
#: conversations tirees par plateforme
PER_PLATFORM = 3
#: sous ce taux de messages retrouves, la plateforme est declaree critique
CRITICAL_RETENTION = 0.5

#: taille minimale (messages) pour qu'une conversation soit un bon canari
MIN_MESSAGES = 8
#: conversations tirees par plateforme
PER_PLATFORM = 3
#: sous ce taux de messages retrouves, la plateforme est declaree critique
CRITICAL_RETENTION = 0.5


def pick_conversations(
    entries: Sequence[Dict[str, Any]],
    count: int = PER_PLATFORM,
    min_messages: int = MIN_MESSAGES,
    seed: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Echantillon aleatoire (reproductible) de conversations assez fournies.

    Les petites conversations sont ecartees : elles n'exercent pas assez de
    capacites du parseur pour servir de canari.
    """
    candidates = [
        entry for entry in entries
        if entry.get("conversation_id")
        and int(entry.get("message_count") or 0) >= min_messages
    ]
    if not candidates:
        return []
    return random.Random(seed).sample(candidates, min(count, len(candidates)))


def compare_conversations(
    archived: Dict[str, Any], fresh: Dict[str, Any]
) -> Dict[str, Any]:
    """Metriques de la version re-scrapee vs l'archivee : pertes et retention.

    `retention` = messages retrouves / messages archives (peut depasser 1 si la
    conversation a continue depuis l'archive).
    """
    old = conversation_metrics(archived)
    new = conversation_metrics(fresh)
    old_messages, new_messages = old["messages"], new["messages"]
    retention = round(new_messages / old_messages, 3) if old_messages else None
    lost = regressions(new, old)
    if new_messages <= 0:
        status = "vide"
    elif lost or (retention is not None and retention < 1.0):
        status = "degrade"
    else:
        status = "ok"
    return {
        "old_messages": old_messages,
        "new_messages": new_messages,
        "retention": retention,
        "lost": lost,
        "status": status,
        "old": old,
        "new": new,
    }


def platform_verdict(conversations: Sequence[Dict[str, Any]]) -> str:
    """Verdict d'une plateforme : `ok`, `attention`, `critique` ou `inconnu`.

    Les conversations `ignore` (limite de debit, indisponible) ne comptent pas :
    un rate-limit ne doit pas passer pour une regression de parseur. Un echec
    isole reste une `attention` ; des echecs majoritaires ou une retention
    catastrophique sont `critique`.
    """
    usable = [item for item in conversations if item.get("status") != "ignore"]
    if not usable:
        return "inconnu"
    broken = [item for item in usable if item.get("status") in ("echec", "vide")]
    if len(broken) * 2 >= len(usable):
        return "critique"
    retentions = [
        item["retention"] for item in usable
        if item.get("retention") is not None
    ]
    if retentions and min(retentions) < CRITICAL_RETENTION:
        return "critique"
    if broken or any(item.get("status") == "degrade" for item in usable):
        return "attention"
    return "ok"


def summarize(
    platform: str, conversations: Sequence[Dict[str, Any]], **meta: Any
) -> Dict[str, Any]:
    """Rapport d'une plateforme (ecrit dans `exports/<platform>/recheck.json`)."""
    usable = [item for item in conversations if item.get("status") != "ignore"]
    retentions = [
        item["retention"] for item in usable
        if item.get("retention") is not None
    ]
    return {
        "platform": platform,
        "checked_at": now_iso_z(),
        "verdict": platform_verdict(conversations),
        "checked": len(conversations),
        "compared": len(usable),
        "ok": sum(1 for i in usable if i.get("status") == "ok"),
        "degraded": sum(1 for i in usable if i.get("status") == "degrade"),
        "failed": sum(1 for i in usable if i.get("status") in ("echec", "vide")),
        "ignored": sum(1 for i in conversations if i.get("status") == "ignore"),
        "retention": (
            round(sum(retentions) / len(retentions), 3) if retentions else None
        ),
        "conversations": list(conversations),
        **meta,
    }


def read_report(exports_dir: Any, platform: str) -> Optional[Dict[str, Any]]:
    """Rapport `recheck.json` d'une plateforme, en forme stable pour l'API/MCP.

    Rend None si le rapport n'existe pas (ou est illisible).
    """
    path = Path(exports_dir) / platform / RECHECK_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "platform": platform,
        "verdict": data.get("verdict") or "inconnu",
        "checked": data.get("checked") or 0,
        "compared": data.get("compared", data.get("checked") or 0),
        "ignored": data.get("ignored") or 0,
        "ok": data.get("ok") or 0,
        "degraded": data.get("degraded") or 0,
        "failed": data.get("failed") or 0,
        "retention": data.get("retention"),
        "checked_at": data.get("checked_at"),
        "seed": data.get("seed"),
        "issues": [
            {
                "title": item.get("title") or item.get("conversation_id"),
                "conversation_id": item.get("conversation_id"),
                "status": item.get("status"),
                "reason": item.get("error")
                or ", ".join(item.get("lost") or [])
                or None,
                "retention": item.get("retention"),
                "old_messages": item.get("old_messages"),
                "new_messages": item.get("new_messages"),
            }
            for item in (data.get("conversations") or [])
            if item.get("status") != "ok"
        ],
    }


def _failure(
    conversation_id: str, entry: Dict[str, Any], status: str, error: str,
    retention: Optional[float] = None,
) -> Dict[str, Any]:
    """Entree de rapport pour une conversation non comparee."""
    return {
        "conversation_id": conversation_id,
        "title": entry.get("title") or "",
        "status": status,
        "error": error,
        "old_messages": entry.get("message_count"),
        "new_messages": 0,
        "retention": retention,
        "lost": [],
    }


def run_platform_recheck(
    config: Dict[str, Any],
    platform: str,
    per_platform: int = PER_PLATFORM,
    min_messages: int = MIN_MESSAGES,
    seed: int = 0,
    orchestrator: Any = None,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> Optional[Dict[str, Any]]:
    """Re-scrape un echantillon d'une plateforme et rend son rapport.

    Rien n'est ecrit dans l'archive : seuls les rapports `recheck.json` le sont.
    Rend None si l'inventaire manque ou si aucun candidat ne passe le filtre.
    """
    # imports paresseux : ce module reste leger pour les serveurs (web, MCP)
    from ..orchestrator import Orchestrator, resolve_engine
    from ..schema import ConversationRef
    from ..services.base import (
        BlockedError,
        ConversationUnavailableError,
        EmptyConversationError,
        RateLimitedError,
        ServiceNotLoggedIn,
    )
    from .urls import conversation_url

    ignored_errors = (
        RateLimitedError,
        BlockedError,
        ServiceNotLoggedIn,
        ConversationUnavailableError,
    )
    stop_errors = (RateLimitedError, BlockedError, ServiceNotLoggedIn)

    output_dir = Path(config.get("output_dir", "exports"))
    list_path = output_dir / platform / CONVERSATION_LIST_NAME
    if not list_path.is_file():
        log(f"{platform}: pas d'inventaire ({list_path})")
        return None
    try:
        entries = json.loads(list_path.read_text(encoding="utf-8")).get("conversations") or []
    except (OSError, json.JSONDecodeError) as exc:
        log(f"{platform}: inventaire illisible ({exc})")
        return None
    picked = pick_conversations(entries, per_platform, min_messages, seed)
    if not picked:
        log(f"{platform}: aucune conversation >= {min_messages} messages")
        return None

    orchestrator = orchestrator or Orchestrator(config)
    cls = orchestrator.registry.get(platform)
    if cls is None:
        log(f"{platform}: service inconnu")
        return None
    service_config = (config.get("services") or {}).get(platform) or {}
    session, service = orchestrator._new_session(
        platform, cls, service_config, resolve_engine(platform, config)
    )
    results: List[Dict[str, Any]] = []
    try:
        for entry in picked:
            conversation_id = str(entry["conversation_id"])
            archived_path = find_export_by_id(output_dir, platform, conversation_id)
            label = f"{conversation_id[:10]} « {(entry.get('title') or '')[:40]} »"
            if archived_path is None:
                log(f"  {label}: pas d'archive, ignore")
                continue
            ref = ConversationRef(
                service=platform,
                id=conversation_id,
                url=entry.get("url") or conversation_url(platform, conversation_id),
                title=entry.get("title"),
            )
            try:
                conversation, _html = service.export_conversation_with_html(ref)
                service.take_raw_payload()  # le payload brut n'est pas archive ici
                archived = json.loads(archived_path.read_text(encoding="utf-8"))
                comparison = compare_conversations(
                    archived, conversation.to_dict(validate=False)
                )
                comparison.update({
                    "conversation_id": conversation_id,
                    "title": archived.get("title") or entry.get("title") or "",
                })
                results.append(comparison)
                counts = (
                    f"{comparison['new_messages']}/{comparison['old_messages']} messages"
                    if comparison["retention"] is not None else "?"
                )
                lost = ", ".join(comparison["lost"]) or "-"
                log(f"  [{comparison['status']:7}] {label} {counts} | perdu: {lost}")
            except EmptyConversationError as exc:
                results.append(_failure(
                    conversation_id, entry, "vide",
                    f"aucun message relu ({type(exc).__name__})", retention=0.0,
                ))
                log(f"  [vide   ] {label} aucun message relu")
            except ignored_errors as exc:
                results.append(_failure(
                    conversation_id, entry, "ignore",
                    f"{type(exc).__name__}: {exc}"[:200],
                ))
                log(f"  [ignore ] {label} {type(exc).__name__}")
                if isinstance(exc, stop_errors):
                    log(f"  {platform}: arret (limite ou session) — relancer plus tard")
                    break
            except Exception as exc:  # noqa: BLE001
                results.append(_failure(
                    conversation_id, entry, "echec",
                    f"{type(exc).__name__}: {exc}"[:200],
                ))
                log(f"  [echec  ] {label} {type(exc).__name__}: {exc}")
    finally:
        orchestrator._close(session)

    report = summarize(
        platform, results,
        seed=seed, per_platform=per_platform, min_messages=min_messages,
    )
    if not dry_run:
        write_json_atomic(output_dir / platform / RECHECK_NAME, report)
    return report


# -- execution en tache de fond (page web, serveur MCP) -----------------------

class RecheckJob(BackgroundJob):
    """Controle de parseur en tache de fond (un seul job a la fois).

    Le verrou est celui du cron (`/tmp/aicv-run.lock`) : un test refuse de
    demarrer pendant un scrape, et inversement.
    """

    label = "controle de parseur"
    options = {"per_platform": PER_PLATFORM, "min_messages": MIN_MESSAGES, "seed": 0}
    default_runner = staticmethod(run_platform_recheck)

    def start(
        self,
        config: Dict[str, Any],
        platforms: Sequence[str],
        per_platform: int = PER_PLATFORM,
        min_messages: int = MIN_MESSAGES,
        seed: int = 0,
    ) -> Dict[str, Any]:
        return super().start(
            config, platforms,
            per_platform=per_platform, min_messages=min_messages, seed=seed,
        )

    def format_done(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Resultat compact d'une plateforme (verdict + defaillances)."""
        return {
            "platform": entry.get("platform"),
            "verdict": entry.get("verdict"),
            "retention": entry.get("retention"),
            "checked": entry.get("checked"),
            "compared": entry.get("compared"),
            "ignored": entry.get("ignored"),
            "ok": entry.get("ok"),
            "degraded": entry.get("degraded"),
            "failed": entry.get("failed"),
            "checked_at": entry.get("checked_at"),
            "issues": [
                {
                    "title": item.get("title") or item.get("conversation_id"),
                    "status": item.get("status"),
                    "reason": item.get("error")
                    or ", ".join(item.get("lost") or [])
                    or None,
                    "retention": item.get("retention"),
                    "old_messages": item.get("old_messages"),
                    "new_messages": item.get("new_messages"),
                }
                for item in (entry.get("conversations") or [])
                if item.get("status") != "ok"
            ],
        }
