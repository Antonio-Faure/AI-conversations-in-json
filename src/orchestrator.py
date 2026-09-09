"""Workflow principal : pipeline de scraping par service vers exports/.

Orchestration:
  1. charge config.yaml (avec defaults fusionnes)
  2. pour chaque service demande : session navigateur (profil persistant)
  3. decouverte des conversations via la sidebar + parser
  4. scraping + parsing de chaque conversation -> Conversation standardisee
  5. ecriture exports/<date>/<service>/<id>.json (atomique) + etat incrementatif

Les tests injectent `registry` (classes factices) et `browser_factory` pour
exercer le pipeline sans navigateur.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from .browser import BrowserSession
from .parsers.base import ParseError
from .schema import Conversation, ConversationRef, SchemaError, parse_iso
from .services.base import BaseService, BlockedError, ServiceNotLoggedIn
from .utils.file_utils import (
    export_path,
    load_state,
    now_iso_z,
    record_state,
    save_state,
    slugify,
    validate_date_arg,
    write_json_atomic,
)
from .utils.logging import get_logger, log_fields

log = get_logger("orchestrator")

DEFAULT_CONFIG: Dict[str, Any] = {
    "output_dir": "exports",
    "profile_dir": ".profiles",
    "state_file": ".state/state.json",
    "log_file": "logs/aicv.jsonl",
    "screenshot_dir": "logs",  # captures de debug (0 conversation, DOM inattendu)
    "headless": False,
    "timeout_ms": 45000,
    # moteur navigateur : auto|playwright|botasaurus
    # auto = playwright, avec bascule botasaurus si challenge anti-bot
    "engine": "auto",
    "botasaurus": {
        "chrome_executable_path": "",  # auto-detecte si vide
        "enable_xvfb": True,           # Chrome headful dans Xvfb (anti-headless-detect)
    },
    "scroll": {"max_rounds": 60, "pause_ms": 700, "stable_rounds": 3},
    "sidebar": {"max_rounds": 25, "pause_ms": 500},
    "services": {
        "chatgpt": {"enabled": True, "url": "https://chatgpt.com/"},
        "claude": {"enabled": True, "url": "https://claude.ai/chats"},
        "gemini": {"enabled": True, "url": "https://gemini.google.com/app"},
        "perplexity": {"enabled": True, "url": "https://www.perplexity.ai/"},
    },
    "schedule": {"enabled": True, "time": "03:30"},
}


def resolve_engine(service_name: str, config: Dict[str, Any]) -> str:
    """Moteur d'un service : override par service, sinon config globale.

    auto -> playwright (la bascule botasaurus est geree par l'orchestrateur
    en cas de BlockedError). Retourne toujours auto|playwright|botasaurus.
    """
    svc = (config.get("services") or {}).get(service_name) or {}
    engine = str(svc.get("engine") or config.get("engine") or "auto").lower()
    return engine if engine in ("auto", "playwright", "botasaurus") else "auto"


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """config.yaml (si present) fusionne sur les defaults."""
    config = dict(DEFAULT_CONFIG)
    if path is None:
        candidate = Path("config.yaml")
        path = candidate if candidate.exists() else None
    if path is not None:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"config invalide (attendu un mapping): {path}")
        config = deep_merge(config, data)
    return config


def default_registry() -> Dict[str, type]:
    from .services import SERVICE_CLASSES

    return dict(SERVICE_CLASSES)


def default_browser_factory(
    profile_dir: Path, service: str, config: Dict[str, Any]
) -> BrowserSession:
    """Cree la session du moteur demande (playwright par defaut).

    La cle `_engine` (posee par l'orchestrateur lors d'une bascule) prime sur
    `engine` ; `auto` est resolu en playwright ici.
    """
    engine = str(config.get("_engine") or config.get("engine") or "auto").lower()
    common = dict(
        profile_dir=profile_dir / slugify(service, 40),
        headless=bool(config.get("headless", False)),
        timeout_ms=int(config.get("timeout_ms", 45000)),
    )
    if engine == "botasaurus":
        from .browser_botasaurus import BotasaurusSession

        bota_cfg = config.get("botasaurus") or {}
        return BotasaurusSession(
            chrome_executable_path=bota_cfg.get("chrome_executable_path") or None,
            enable_xvfb=bool(bota_cfg.get("enable_xvfb", False)),
            **common,
        )
    return BrowserSession(**common)


@dataclass
class ServiceResult:
    service: str
    exported: List[Path] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    out_of_range: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped


@dataclass
class RunSummary:
    date: str
    services: Dict[str, ServiceResult] = field(default_factory=dict)

    @property
    def total_exported(self) -> int:
        return sum(len(r.exported) for r in self.services.values())

    @property
    def has_failures(self) -> bool:
        return any(r.failed or (r.skipped and r.skip_reason != "disabled")
                   for r in self.services.values())


class Orchestrator:
    def __init__(
        self,
        config: Dict[str, Any],
        registry: Optional[Dict[str, type]] = None,
        browser_factory: Optional[Callable[[Path, str, Dict[str, Any]], Any]] = None,
    ):
        self.config = config
        self.registry = registry if registry is not None else default_registry()
        self.browser_factory = browser_factory or default_browser_factory
        self.output_dir = Path(config.get("output_dir", "exports"))
        self.profile_dir = Path(config.get("profile_dir", ".profiles"))
        self.state_file = Path(config.get("state_file", ".state/state.json"))
        self.state = load_state(self.state_file)

    # -- helpers ------------------------------------------------------------

    def _service_names(self, requested: Optional[List[str]], all_services: bool = False) -> List[str]:
        configured = self.config.get("services", {})
        if requested:
            unknown = [s for s in requested if s not in self.registry]
            if unknown:
                raise ValueError(
                    f"service(s) inconnu(s): {', '.join(unknown)} "
                    f"(disponibles: {', '.join(sorted(self.registry))})"
                )
            return list(requested)
        if all_services:
            return sorted(self.registry)
        return [
            s
            for s in self.registry
            if s in configured and configured.get(s, {}).get("enabled", True)
        ]

    def _state_entry(self, service: str, conv_id: str) -> Optional[Dict[str, Any]]:
        return self.state.get("services", {}).get(service, {}).get(conv_id)

    def _export_date(self, conv: Conversation, fallback: str) -> str:
        anchor = conv.last_message_at or conv.started_at
        dt = parse_iso(anchor)
        return dt.strftime("%Y-%m-%d") if dt else fallback

    def _matches_date(self, conv: Conversation, date_str: str) -> bool:
        return self._export_date(conv, date_str) == date_str

    # -- pipeline --------------------------------------------------------------

    def _start_service(
        self,
        service_name: str,
        cls: type,
        svc_config: Dict[str, Any],
        engine: str,
    ) -> tuple:
        """Session + service pour un moteur donne ('' = config courante)."""
        factory_config = self.config
        if engine:
            factory_config = dict(self.config)
            factory_config["_engine"] = engine
        session = self.browser_factory(self.profile_dir, service_name, factory_config)
        service: BaseService = cls(session, self.config)
        if svc_config.get("url"):
            service.home_url = svc_config["url"]
        return session, service

    def run_service(
        self,
        service_name: str,
        date_str: str,
        limit: Optional[int] = None,
        force: bool = False,
        ignore_enabled: bool = False,
        filter_date: bool = False,
    ) -> ServiceResult:
        result = ServiceResult(service=service_name)
        svc_config = self.config.get("services", {}).get(service_name, {})
        cls = self.registry.get(service_name)
        if cls is None:
            result.skipped = True
            result.skip_reason = "unknown service"
            return result
        if not ignore_enabled and not svc_config.get("enabled", True):
            result.skipped = True
            result.skip_reason = "disabled"
            return result

        engine = resolve_engine(service_name, self.config)
        attempt_engine = "playwright" if engine == "auto" else engine
        session, service = self._start_service(service_name, cls, svc_config, attempt_engine)

        # -- decouverte (bascule botasaurus une fois si auto + bloque) ---------
        refs = None
        while True:
            try:
                refs = service.list_conversations(limit=limit)
                break
            except ServiceNotLoggedIn as exc:
                log.warning(str(exc))
                result.skipped = True
                result.skip_reason = str(exc)
                self._close(session)
                return result
            except BlockedError as exc:
                if engine == "auto" and attempt_engine != "botasaurus":
                    log_fields(
                        log, 30,
                        f"{service_name}: bloque par anti-bot -> nouvelle tentative "
                        f"avec le moteur botasaurus",
                        extra={"service": service_name, "error": str(exc)},
                    )
                    attempt_engine = "botasaurus"
                    self._close(session)
                    session, service = self._start_service(
                        service_name, cls, svc_config, attempt_engine
                    )
                    continue
                log.warning(str(exc))
                result.skipped = True
                result.skip_reason = str(exc)
                self._close(session)
                return result
            except Exception as exc:
                log.exception(f"{service_name}: echec de decouverte")
                result.failed.append(f"discovery: {exc}")
                self._close(session)
                return result

        log_fields(
            log,
            20,
            f"{service_name}: {len(refs)} conversation(s) a traiter",
            extra={"service": service_name, "date": date_str, "engine": attempt_engine},
        )
        for ref in refs:
            status = self._process_ref(service, result, ref, date_str, force, filter_date)
            if status == "blocked" and engine == "auto" and attempt_engine != "botasaurus":
                # bascule botasaurus : session neuve, on retente cette conv
                log_fields(
                    log, 30,
                    f"{service_name}: bloque sur {ref.id} -> bascule botasaurus",
                    extra={"service": service_name, "conversation_id": ref.id},
                )
                attempt_engine = "botasaurus"
                self._close(session)
                session, service = self._start_service(
                    service_name, cls, svc_config, attempt_engine
                )
                status = self._process_ref(service, result, ref, date_str, force, filter_date)
            if status == "blocked":
                result.failed.append(ref.id)
            elif status == "stop":
                break
        self._close(session)
        return result

    def _process_ref(
        self,
        service: BaseService,
        result: ServiceResult,
        ref: ConversationRef,
        date_str: str,
        force: bool,
        filter_date: bool,
    ) -> Optional[str]:
        """Traite une conversation. Retourne 'blocked', 'stop' ou None."""
        try:
            self._process_conversation(service, result, ref, date_str, force, filter_date)
            return None
        except BlockedError as exc:
            log_fields(
                log, logging.ERROR, f"{service.name}: challenge anti-bot",
                extra={"conversation_id": ref.id, "error": str(exc)},
            )
            return "blocked"
        except ServiceNotLoggedIn as exc:
            log_fields(
                log, logging.ERROR, f"{service.name}: session perdue",
                extra={"conversation_id": ref.id, "error": str(exc)},
            )
            result.failed.append(ref.id)
            return "stop"
        except (ParseError, SchemaError) as exc:
            log_fields(
                log, logging.ERROR, f"{service.name}: parse/validate echoue",
                extra={"conversation_id": ref.id, "error": str(exc)},
            )
            result.failed.append(ref.id)
            return None
        except Exception:
            log.exception(f"{service.name}: erreur inattendue sur {ref.id}")
            result.failed.append(ref.id)
            return None

    def _process_conversation(
        self,
        service: BaseService,
        result: ServiceResult,
        ref: ConversationRef,
        date_str: str,
        force: bool,
        filter_date: bool,
    ) -> None:
        conv = service.export_conversation(ref)
        conv.exported_at = now_iso_z()

        conv_date = self._export_date(conv, date_str)
        if filter_date and conv_date != date_str:
            result.out_of_range.append(ref.id)
            return

        entry = self._state_entry(service.name, conv.conversation_id)
        if (
            entry
            and not force
            and entry.get("message_count") == len(conv.messages)
            and entry.get("last_message_at") in (None, conv.last_message_at)
        ):
            log_fields(log, logging.DEBUG, f"{service.name}: inchange",
                       extra={"conversation_id": conv.conversation_id})
            result.unchanged.append(ref.id)
            return

        path = export_path(self.output_dir, conv_date, service.name, conv.conversation_id)
        write_json_atomic(path, conv.to_dict(validate=False))
        record_state(
            self.state,
            service.name,
            conv.conversation_id,
            title=conv.title,
            message_count=len(conv.messages),
            path=path,
            last_message_at=conv.last_message_at,
        )
        save_state(self.state_file, self.state)
        log_fields(
            log,
            20,
            f"{service.name}: exportee",
            extra={
                "conversation_id": conv.conversation_id,
                "messages": len(conv.messages),
                "path": str(path),
            },
        )
        result.exported.append(path)

    @staticmethod
    def _close(session: Any) -> None:
        try:
            session.close()
        except Exception:
            pass

    def run(
        self,
        services: Optional[List[str]] = None,
        date: Optional[str] = None,
        all_services: bool = False,
        limit: Optional[int] = None,
        force: bool = False,
        filter_date: Optional[bool] = None,
    ) -> RunSummary:
        from .utils.file_utils import today_str

        date_str = validate_date_arg(date) if date else today_str()
        run_all = all_services or (services is None)
        names = self._service_names(services, all_services=run_all)
        # un --date explicite filtre sur cette date ; sinon on exporte tout
        # (comportement quotidien : chaque conv part dans son propre jour).
        do_filter = filter_date if filter_date is not None else bool(date)
        summary = RunSummary(date=date_str)
        log_fields(log, 20, "run start",
                   extra={"services": names, "date": date_str, "force": force,
                          "filter_date": do_filter})
        for name in names:
            summary.services[name] = self.run_service(
                name, date_str, limit=limit, force=force, filter_date=do_filter
            )
        log_fields(
            log,
            20,
            "run done",
            extra={
                "exported": summary.total_exported,
                "services": {n: {"exported": len(r.exported), "failed": len(r.failed),
                                 "skipped": r.skip_reason} for n, r in summary.services.items()},
            },
        )
        return summary
