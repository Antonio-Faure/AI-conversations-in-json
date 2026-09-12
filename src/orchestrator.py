"""Workflow principal : scraping d'un chatbot vers exports/<platform>/.

Architecture:
  exports/
    chatgpt/
      conversation_list.json      # inventaire de toutes les conversations
      <nom conversation>.json     # messages standardises
      <nom conversation>.html     # page complete (brut)

Deux modes:
  - monthly (--full) : liste TOUTES les conversations et les scrape toutes
  - daily            : scrape les conversations inconnues + les 20 plus recentes
                       (union), puis met a jour conversation_list.json

Les fichiers existants sont ecrases (la conversation a pu evoluer).
Aucun log fichier, aucun dossier par session : seule la sortie de conversation
est conservee.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import yaml

from .fingerprint import get_fingerprint

from .browser import BrowserSession
from .parsers.base import ParseError
from .schema import ConversationRef, SchemaError
from .services.base import (
    BaseService,
    BlockedError,
    EmptyConversationError,
    NoopSession,
    ServiceNotLoggedIn,
)
from .utils.file_utils import (
    conversation_list_path,
    conversation_paths,
    load_conversation_list,
    merge_conversation_json,
    now_iso_z,
    read_json,
    save_conversation_list,
    slugify,
    unique_filename,
    write_json_atomic,
    write_text_atomic,
)
from .utils.images import IMAGES_SUBDIR, download_service_images
from .utils.logging import get_logger, log_fields

log = get_logger("orchestrator")


def _fold(value: str) -> str:
    """Minuscule sans accents (comparaison de titres)."""
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()

MODES = ("daily", "monthly")
DAILY_RECENT = 20

DEFAULT_CONFIG: Dict[str, Any] = {
    # chemins absolus (depot de reference)
    "output_dir": "/home/odin/Documents/code/AI-conversations-in-json/exports",
    "profile_dir": "/home/odin/Documents/code/AI-conversations-in-json/profiles",
    "cookies_dir": "/home/odin/Documents/code/AI-conversations-in-json/cookies",
    "rag_db": "/home/odin/Documents/code/AI-conversations-in-json/rag/messages.db",
    "rag_model": "BAAI/bge-m3",
    # pas de logs sur disque : console uniquement
    "screenshot_dir": "",
    # capture un screenshot par message (tour centre) dans exports/.../screenshots/
    "screenshots": False,
    "headless": True,
    "timeout_ms": 45000,
    "parallel": 1,
    "pacing_ms": 0,
    # moteur navigateur : auto|playwright|botasaurus
    # auto = playwright, avec bascule botasaurus si challenge anti-bot
    "engine": "auto",
    "botasaurus": {
        "chrome_executable_path": "",  # auto-detecte si vide
        "enable_xvfb": True,           # Chrome headful dans Xvfb (anti-headless-detect)
    },
    "scroll": {"max_rounds": 60, "pause_ms": 700, "stable_rounds": 3},
    # sidebar lazy-load : rounds+stabilite generes pour charger l'historique complet
    "sidebar": {"max_rounds": 40, "pause_ms": 700, "stable_rounds": 4},
    "services": {
        "chatgpt": {"enabled": True, "url": "https://chatgpt.com/"},
        # Claude : Cloudflare bloque Playwright -> moteur botasaurus
        "claude": {"enabled": True, "url": "https://claude.ai/chats", "engine": "botasaurus"},
        "gemini": {"enabled": True, "url": "https://gemini.google.com/app"},
        "perplexity": {"enabled": True, "url": "https://www.perplexity.ai/", "engine": "botasaurus"},
        # Grok : API HTTP + cookies (pas de navigateur)
        "grok": {"enabled": True, "url": "https://grok.com/"},
        "mistral": {"enabled": True, "url": "https://chat.mistral.ai/work", "engine": "botasaurus"},
    },
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
) -> Any:
    """Cree la session du moteur demande (playwright par defaut).

    La cle `_engine` (posee par l'orchestrateur lors d'une bascule) prime sur
    `engine` ; `auto` est resolu en playwright ici.
    """
    engine = str(config.get("_engine") or config.get("engine") or "auto").lower()
    service_profile = profile_dir / slugify(service, 40)
    # fingerprint stable et isole par profil (UA, fenetre, langue)
    fingerprint = get_fingerprint(service, service_profile)
    common = dict(
        profile_dir=service_profile,
        headless=bool(config.get("headless", False)),
        timeout_ms=int(config.get("timeout_ms", 45000)),
        fingerprint=fingerprint,
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
    discovered: int = 0
    targets: int = 0
    exported: List[Path] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    patched: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.skipped_reason is None


@dataclass
class RunSummary:
    mode: str
    services: Dict[str, ServiceResult] = field(default_factory=dict)

    @property
    def total_exported(self) -> int:
        return sum(len(r.exported) for r in self.services.values())

    @property
    def has_failures(self) -> bool:
        return any(r.failed or (r.skipped_reason and r.skipped_reason != "disabled")
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
        self.profile_dir = Path(config.get("profile_dir", "profiles"))

    # -- helpers ------------------------------------------------------------

    def _service_names(self, requested: Optional[List[str]] = None) -> List[str]:
        """Services a executer.

        Si `requested` est fourni (--service) : uniquement ceux-la.
        Sinon : tous les services actifs de la config (enabled != false).
        """
        if requested:
            unknown = [s for s in requested if s not in self.registry]
            if unknown:
                raise ValueError(
                    f"service(s) inconnu(s): {', '.join(unknown)} "
                    f"(disponibles: {', '.join(sorted(self.registry))})"
                )
            return sorted(set(requested))
        configured = self.config.get("services", {})
        return sorted(
            name
            for name in self.registry
            if configured.get(name, {}).get("enabled", True)
        )

    def _new_session(self, name: str, cls: type, svc_config: Dict[str, Any], engine: str) -> tuple:
        factory_config = self.config
        if engine:
            factory_config = dict(self.config)
            factory_config["_engine"] = engine
        if getattr(cls, "uses_browser", True):
            session = self.browser_factory(self.profile_dir, name, factory_config)
        else:
            # service API HTTP + cookies : aucun navigateur a ouvrir
            session = NoopSession()
        service: BaseService = cls(session, self.config)
        if svc_config.get("url"):
            service.home_url = svc_config["url"]
        return session, service

    @staticmethod
    def _close(session: Any) -> None:
        try:
            session.close()
        except Exception:
            pass

    # -- pipeline -----------------------------------------------------------

    def _discover(self, result: ServiceResult, name: str, session, service, engine: str,
                  attempt_engine: str, svc_config: Dict[str, Any]):
        """Decouverte ; bascule botasaurus une fois si auto + bloque.

        Retourne (session, service, refs, attempt_engine) ou None si le service
        doit etre arrete. La session concernee est fermee en interne sur echec.
        """
        while True:
            try:
                refs = service.list_conversations()
                result.discovered = len(refs)
                return session, service, refs, attempt_engine
            except ServiceNotLoggedIn as exc:
                log.warning(str(exc))
                result.skipped_reason = str(exc)
                self._close(session)
                return None
            except BlockedError as exc:
                if engine == "auto" and attempt_engine != "botasaurus":
                    log_fields(
                        log, 30,
                        f"{name}: bloque par anti-bot -> tentative avec botasaurus",
                        extra={"service": name, "error": str(exc)},
                    )
                    attempt_engine = "botasaurus"
                    self._close(session)
                    session, service = self._new_session(
                        name, self.registry[name], svc_config, attempt_engine
                    )
                    continue
                log.warning(str(exc))
                result.skipped_reason = str(exc)
                self._close(session)
                return None
            except Exception as exc:  # noqa: BLE001
                log.exception(f"{name}: echec de decouverte")
                result.failed.append(f"discovery: {exc}")
                self._close(session)
                return None

    @staticmethod
    def _boost_refs(
        service: BaseService, refs: List[ConversationRef], target: int
    ) -> List[ConversationRef]:
        """Union de plusieurs decouvertes tant que la liste progresse.

        Les sidebars virtualisees (ChatGPT) renvoient parfois une liste
        partielle ; on relance la decouverte et on fusionne par id.
        """
        if not target or len(refs) >= target:
            return refs
        merged = {ref.id: ref for ref in refs}
        for _ in range(3):
            try:
                more = service.list_conversations()
            except Exception:  # noqa: BLE001
                break
            before = len(merged)
            for ref in more:
                merged.setdefault(ref.id, ref)
            if len(merged) == before or len(merged) >= target:
                break
        return list(merged.values())

    def _scrape_one(
        self,
        service: BaseService,
        result: ServiceResult,
        ref: ConversationRef,
        index: Dict[str, Dict[str, Any]],
        used: Dict[str, str],
    ) -> Optional[str]:
        """Scrape une conversation, ecrit JSON + HTML, met a jour l'inventaire.

        Retourne 'blocked', 'stop' ou None.
        """
        try:
            conv, html = service.export_conversation_with_html(ref)
        except BlockedError as exc:
            log_fields(
                log, logging.ERROR, f"{service.name}: challenge anti-bot",
                extra={"conversation_id": ref.id, "error": str(exc)},
            )
            return "blocked"
        except EmptyConversationError as exc:
            log_fields(
                log, logging.WARNING, f"{service.name}: conversation ignoree",
                extra={"conversation_id": ref.id, "reason": str(exc)},
            )
            result.skipped.append(ref.id)
            return None
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

        conv.exported_at = now_iso_z()
        conv.platform = service.name
        if not conv.url:
            try:
                conv.url = service.conversation_url(ref)
            except Exception:  # noqa: BLE001
                conv.url = ref.url
        entry = index.get(ref.id) or {}
        title = conv.title or entry.get("title") or ref.title or ""
        stem = entry.get("file") or unique_filename(title, ref.id, used)
        used.setdefault(stem, ref.id)

        json_path, html_path = conversation_paths(self.output_dir, service.name, stem)
        # images : telechargement local pendant que la session est encore ouverte
        try:
            download_service_images(conv, service, json_path.parent / IMAGES_SUBDIR)
        except Exception:  # noqa: BLE001
            log.debug("telechargement des images ignore", exc_info=True)
        # JSON intelligent : identique -> pas d'ecriture ; nouveaux messages -> patch
        payload = conv.to_dict(validate=False)
        existing = read_json(json_path, default=None)
        merged, status, appended = merge_conversation_json(existing, payload)
        if status == "unchanged":
            result.unchanged.append(ref.id)
        else:
            write_json_atomic(json_path, merged)
            result.exported.append(json_path)
            if status == "patched":
                result.patched.append(ref.id)
        # HTML : toujours ecrase (rendu, aucune donnee perdue)
        write_text_atomic(html_path, html)
        # screenshots par message (optionnel ; session encore ouverte)
        if self.config.get("screenshots"):
            try:
                service.capture_message_screenshots(
                    conv, json_path.parent / "screenshots" / stem
                )
            except Exception:  # noqa: BLE001
                log.debug("capture des screenshots ignoree", exc_info=True)

        index[ref.id] = {
            "conversation_id": ref.id,
            "title": title,
            "url": conv.url,
            "message_count": len(conv.messages),
            "last_message_at": conv.last_message_at,
            "has_code": conv.has_code,
            "scraped": True,
            "file": stem,
            "scraped_at": now_iso_z(),
        }
        log_fields(
            log, 20, f"{service.name}: {status}",
            extra={
                "conversation_id": ref.id,
                "messages": len(conv.messages),
                "appended": appended,
                "file": stem,
            },
        )
        return None

    def _select_targets(
        self,
        refs: List[ConversationRef],
        index: Dict[str, Dict[str, Any]],
        mode: str,
        limit: Optional[int],
    ) -> List[ConversationRef]:
        if mode == "monthly":
            targets = list(refs)
        else:
            # inconnues (jamais scrapees) + 20 plus recentes (ordre sidebar)
            unknown = [
                ref for ref in refs if not (index.get(ref.id) or {}).get("scraped")
            ]
            selected = {ref.id for ref in unknown}
            selected.update(ref.id for ref in refs[:DAILY_RECENT])
            targets = [ref for ref in refs if ref.id in selected]
        if limit:
            targets = targets[:limit]
        return targets

    def run_service(
        self,
        name: str,
        mode: str,
        limit: Optional[int] = None,
        match: Optional[str] = None,
    ) -> ServiceResult:
        result = ServiceResult(service=name)
        svc_config = self.config.get("services", {}).get(name, {})
        cls = self.registry.get(name)
        if cls is None:
            result.skipped_reason = "unknown service"
            return result
        if not svc_config.get("enabled", True):
            result.skipped_reason = "disabled"
            return result

        engine = resolve_engine(name, self.config)
        attempt_engine = "playwright" if engine == "auto" else engine
        session, service = self._new_session(name, cls, svc_config, attempt_engine)

        list_path = conversation_list_path(self.output_dir, name)
        index = load_conversation_list(list_path)
        known = sum(1 for entry in index.values() if entry.get("scraped"))

        try:
            discovered = self._discover(
                result, name, session, service, engine, attempt_engine, svc_config
            )
            if discovered is None:
                return result
            session, service, refs, attempt_engine = discovered
            # union de plusieurs passes si la liste semble incomplete (sidebars
            # virtualisees/lazy : ChatGPT)
            refs = self._boost_refs(service, refs, known)
            result.discovered = len(refs)
            self._refresh_inventory(index, refs)

            used: Dict[str, str] = {}
            for cid, entry in index.items():
                if entry.get("file"):
                    used[entry["file"]] = cid

            targets = self._select_targets(refs, index, mode, limit)
            if match:
                needle = _fold(match)
                targets = [
                    ref for ref in targets
                    if needle in _fold(ref.title or "") or needle in _fold(ref.id)
                ]
            result.targets = len(targets)
            log_fields(
                log, 20,
                f"{name}: {len(refs)} decouvertes, {len(targets)} a scraper ({mode})",
                extra={"service": name, "mode": mode},
            )

            for ref in targets:
                status = self._scrape_one(service, result, ref, index, used)
                if (
                    status == "blocked"
                    and engine == "auto"
                    and attempt_engine != "botasaurus"
                ):
                    log_fields(
                        log, 30,
                        f"{name}: bloque sur {ref.id} -> bascule botasaurus",
                        extra={"service": name, "conversation_id": ref.id},
                    )
                    attempt_engine = "botasaurus"
                    self._close(service.session)
                    session, service = self._new_session(
                        name, cls, svc_config, attempt_engine
                    )
                    status = self._scrape_one(service, result, ref, index, used)
                if status == "blocked":
                    result.failed.append(ref.id)
                elif status == "stop":
                    break
                # sauvegarde incrementale : l'inventaire survit a une interruption
                save_conversation_list(list_path, name, index)
                pacing = int(
                    svc_config.get("pacing_ms", self.config.get("pacing_ms", 0)) or 0
                )
                pacing = self._jitter_pacing(name, pacing)
                if pacing > 0:
                    service.session.wait_ms(pacing)
        finally:
            self._close(session)

        save_conversation_list(list_path, name, index)
        return result

    @staticmethod
    def _refresh_inventory(
        index: Dict[str, Dict[str, Any]], refs: List[ConversationRef]
    ) -> None:
        """Ajoute les nouvelles conversations ; rafraichit le titre des non scrapees."""
        for ref in refs:
            raw = ref.raw or {}
            entry = index.get(ref.id)
            if entry is None:
                index[ref.id] = {
                    "conversation_id": ref.id,
                    "title": ref.title or "",
                    "url": ref.url or "",
                    "message_count": None,
                    "last_message_at": raw.get("last_message_at"),
                    "has_code": None,
                    "scraped": False,
                    "file": None,
                    "scraped_at": None,
                }
            elif not entry.get("scraped"):
                if ref.title:
                    entry["title"] = ref.title
                if ref.url:
                    entry["url"] = ref.url
                if raw.get("last_message_at"):
                    entry["last_message_at"] = raw["last_message_at"]

    # -- parallelisme (profils isoles) ----------------------------------------

    @staticmethod
    def _jitter_pacing(service: str, base_ms: int) -> int:
        """Delai variable par tete : deux chatbots ne frappent pas au meme rythme."""
        if base_ms <= 0:
            return 0
        rng = random.Random(f"{service}:{int(time.time() // 20)}")
        # +0..base (borne mini 300 ms) : rythmes independants entre services
        return base_ms + rng.randint(0, max(300, base_ms))

    def _domain_of(self, name: str) -> str:
        url = (self.config.get("services", {}).get(name) or {}).get("url") or ""
        return (urlparse(url).netloc or name).lower()

    def _group_by_domain(self, names: List[str]) -> List[List[str]]:
        """Groupes de services par domaine (un groupe = execution sequentielle)."""
        groups: Dict[str, List[str]] = {}
        order: List[str] = []
        for name in names:
            domain = self._domain_of(name)
            if domain not in groups:
                groups[domain] = []
                order.append(domain)
            groups[domain].append(name)
        return [groups[d] for d in order]

    def run(
        self,
        mode: str = "daily",
        limit: Optional[int] = None,
        services: Optional[List[str]] = None,
        parallel: int = 1,
        match: Optional[str] = None,
    ) -> RunSummary:
        if mode not in MODES:
            raise ValueError(f"mode invalide {mode!r} (attendu: {', '.join(MODES)})")
        names = self._service_names(services)
        groups = self._group_by_domain(names)
        summary = RunSummary(mode=mode)
        lock = threading.Lock()

        def run_group(group: List[str]) -> None:
            # sequentiel DANS un groupe ; les groupes (domaines) tournent en parallele
            for name in group:
                result = self.run_service(name, mode, limit=limit, match=match)
                with lock:
                    summary.services[name] = result

        workers = max(1, min(int(parallel or 1), len(groups)))
        log_fields(
            log, 20, "run start",
            extra={"services": names, "mode": mode, "limit": limit,
                   "groups": groups, "parallel": workers},
        )
        if workers == 1:
            for group in groups:
                run_group(group)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                list(executor.map(run_group, groups))
        log_fields(
            log, 20, "run done",
            extra={
                "mode": mode,
                "exported": summary.total_exported,
                "services": {
                    n: {"discovered": r.discovered, "targets": r.targets,
                        "exported": len(r.exported), "unchanged": len(r.unchanged),
                        "patched": len(r.patched), "failed": len(r.failed),
                        "skipped": r.skipped_reason}
                    for n, r in summary.services.items()
                },
            },
        )
        return summary
