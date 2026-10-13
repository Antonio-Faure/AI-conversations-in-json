"""Classe abstraite BaseService : decouverte + scraping via Playwright.

Un service connait son/ses URL, ses selecteurs de sidebar et delague tout le
travail de lecture DOM a son parser (BeautifulSoup, testable sans navigateur).
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence
from urllib.parse import urljoin

from ..parsers.base import LOGIN_URL_PARTS, ParseError
from ..schema import Conversation, ConversationRef
from ..selectors import translate_selector
from ..utils.api_shape import compare_shapes, shape_of
from ..utils.file_utils import now_iso_z, read_json
from ..utils.logging import get_logger, log_fields
from ..utils.screenshots import (
    MAX_STEPS,
    MAX_TURNS,
    SETTLE_MS,
    STABLE_ROUNDS,
    screenshot_reset_body,
    screenshot_step_body,
)

if TYPE_CHECKING:  # evite la dependance playwright pour les tests parsers/schema
    from ..browser import BrowserSession

log = get_logger("services")


class ServiceNotLoggedIn(Exception):
    """Session absente/expirée : ouvrir le service en mode visible pour se connecter."""


class BlockedError(RuntimeError):
    """Challenge anti-bot (Cloudflare) : passer une fois en --headful pour valider."""


class EmptyConversationError(RuntimeError):
    """La conversation ne charge aucun message (vide, tâche, ou app en echec)."""


class ConversationUnavailableError(EmptyConversationError):
    """Conversation inaccessible (supprimee/privee) : ne plus la retenter."""


class RateLimitedError(RuntimeError):
    """Limite de debit persistante : arreter le service proprement (reprise plus tard)."""


class ApiShapeBreakError(RuntimeError):
    """Rupture de forme API (`on_break: stop`) : arreter le service proprement."""


#: dossier des references de forme versionnees (tests/fixtures/api_shapes)
API_SHAPES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "api_shapes"


@dataclass
class ShapeMonitor:
    """Accumulateur de similarite de forme des payloads API d'un run.

    Chaque payload de conversation est compare a la reference versionnee de la
    plateforme (`tests/fixtures/api_shapes/<platform>.shape.json`) : l'ecart est
    score (0-100) et cumule (n, matches, drifts, score min/moyen, cles en
    drift). L'orchestrateur ecrit le bilan dans
    `exports/<platform>/api_health.json` en fin de service.
    """

    platform: str
    #: document de reference (`{"shape": ..., "signature": ...}`) ou None
    reference: Optional[Dict[str, Any]] = None
    threshold: float = 95.0
    on_break: str = "warn"
    enabled: bool = True
    n: int = 0
    matches: int = 0
    drifts: int = 0
    breaks: int = 0
    min_score: Optional[float] = None
    sum_score: float = 0.0
    drift_keys: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def for_service(
        cls, platform: str, config: Optional[Dict[str, Any]] = None
    ) -> "ShapeMonitor":
        """Moniteur d'un service depuis `services.<platform>.api_health`.

        Defauts : active, seuil 95, `on_break: warn`. Une reference absente
        desactive de fait le monitoring (payload non compare).
        """
        svc = ((config or {}).get("services") or {}).get(platform) or {}
        cfg = svc.get("api_health") or {}
        enabled = bool(cfg.get("enabled", True))
        threshold = float(cfg.get("threshold", 95.0) or 95.0)
        on_break = str(cfg.get("on_break", "warn") or "warn").lower()
        reference: Optional[Dict[str, Any]] = None
        if enabled:
            path = API_SHAPES_DIR / f"{platform}.shape.json"
            doc = read_json(path, default=None)
            if isinstance(doc, dict) and isinstance(doc.get("shape"), dict):
                reference = doc
            else:
                log.debug(
                    "%s: reference de forme absente (%s) -> monitoring inactif",
                    platform, path,
                )
        return cls(
            platform=platform,
            reference=reference,
            threshold=threshold,
            on_break=on_break,
            enabled=enabled,
        )

    @property
    def mean_score(self) -> Optional[float]:
        return round(self.sum_score / self.n, 2) if self.n else None

    def record(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Compare un payload a la reference ; met a jour l'accumulateur.

        Retourne le rapport `compare_shapes` (ou None si desactive/sans
        reference).
        """
        if not self.enabled or not self.reference:
            return None
        report = compare_shapes(self.reference["shape"], shape_of(payload))
        score = float(report["score"])
        self.n += 1
        self.sum_score += score
        self.min_score = score if self.min_score is None else min(self.min_score, score)
        if score >= self.threshold:
            self.matches += 1
        else:
            self.drifts += 1
            keys = (
                report["added"]
                + report["removed"]
                + report["type_changes"]
                + report["cardinality_changes"]
                + report["format_changes"]
            )
            for key in keys[:50]:
                self.drift_keys[key] = self.drift_keys.get(key, 0) + 1
        if report["level"] == "BREAK":
            self.breaks += 1
        return report

    def summary(self) -> Dict[str, Any]:
        """Bilan compact (ligne de resume + `api_health.json`)."""
        return {
            "platform": self.platform,
            "n": self.n,
            "matches": self.matches,
            "drifts": self.drifts,
            "breaks": self.breaks,
            "min_score": self.min_score,
            "mean_score": self.mean_score,
            "threshold": self.threshold,
            "on_break": self.on_break,
            "drift_keys": dict(
                sorted(self.drift_keys.items(), key=lambda kv: (-kv[1], kv[0]))
            ),
        }

    def to_dict(self) -> Dict[str, Any]:
        """Document `api_health.json` (bilan + horodatage + signature ref)."""
        out = self.summary()
        out["updated_at"] = now_iso_z()
        out["signature"] = (self.reference or {}).get("signature")
        return out


#: textes des dialogs de rate limite (ChatGPT "Too many requests", etc.)
RATE_LIMIT_MARKERS = ("too many requests", "trop de requ", "temporairement limit")
RATE_LIMIT_DISMISS_SELECTORS = (
    "button:has-text('Got it')",
    "button:has-text('OK')",
    'button:has-text("J\'ai compris")',
)


def _file_digest(path: Path) -> str:
    """Empreinte SHA-1 d'un fichier (pour dedupliquer les screenshots)."""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return f"missing:{path}"


@dataclass
class ScrapedPage:
    html: str
    conversation_id: str
    url: str
    extra: Dict[str, Any] = field(default_factory=dict)


class NoopSession:
    """Session factice pour les services sans navigateur (API HTTP + cookies)."""

    def wait_ms(self, ms: int) -> None:
        pass

    def close(self) -> None:
        pass


class BaseService(ABC):
    """Pipeline commun : list_conversations -> scrape_conversation -> parse."""

    #: False pour les services sans navigateur (API HTTP + cookies, ex: Grok)
    uses_browser: bool = True
    name: str = ""
    home_url: str = ""
    #: page de decouverte (liste des conversations) si differente de home_url
    discover_url: str = ""
    #: selectors qui prouvent que la liste de conversations est chargee
    sidebar_ready_selectors: tuple = ()
    #: parties d'URL de login specifiques au service (complete LOGIN_URL_PARTS)
    login_url_parts: tuple = LOGIN_URL_PARTS
    #: selectors d'ecran de login (bouton "Log in"/"Sign in")
    login_selectors: tuple = (
        "a[href*='login' i]",
        "button:has-text('Log in')",
        "input[type='password']",
    )
    #: marqueurs d'ecran vide (conversation non chargeable : tache, supprimee...)
    empty_chat_selectors: tuple = ()

    #: accumulateur de similarite de forme des payloads API (services API)
    shape_monitor: Optional[ShapeMonitor] = None

    def __init__(self, session: BrowserSession, config: Optional[Dict[str, Any]] = None):
        self.session = session
        self.config = config or {}
        #: payload API brut de la derniere conversation exportee (consomme)
        self._raw_payload: Optional[Any] = None
        self.parser = self.build_parser()

    # -- monitoring de forme API -----------------------------------------------

    def _api_config(self) -> Dict[str, Any]:
        """Config du service (`services.<name>` de la config globale)."""
        services = (self.config or {}).get("services") or {}
        return services.get(self.name) or {}

    def record_api_shape(self, payload: Any) -> Optional[Dict[str, Any]]:
        """Hook de monitoring : forme d'un payload API vs reference versionnee.

        A appeler juste apres chaque fetch de conversation (services API). Met
        a jour le `ShapeMonitor` du run ; WARN/BREAK sont journalises et
        `on_break: "stop"` interrompt le service (`ApiShapeBreakError`).
        """
        if self.shape_monitor is None:
            self.shape_monitor = ShapeMonitor.for_service(self.name, self.config)
        monitor = self.shape_monitor
        report = monitor.record(payload)
        if report is None or report["level"] == "OK":
            return report
        log_fields(
            log,
            30 if report["level"] == "WARN" else 40,
            f"{self.name}: forme API {report['level']} ({report['score']:.1f}%)",
            extra={
                "service": self.name,
                "score": report["score"],
                "added": report["added"][:10],
                "removed": report["removed"][:10],
                "type_changes": report["type_changes"][:10],
                "cardinality_changes": report["cardinality_changes"][:10],
                "format_changes": report["format_changes"][:10],
            },
        )
        if report["level"] == "BREAK" and monitor.on_break == "stop":
            raise ApiShapeBreakError(
                f"{self.name}: rupture de forme API ({report['score']:.1f}%) "
                f"-> service arrete (on_break: stop)"
            )
        return report

    # -- a implementer par service ---------------------------------------------

    @abstractmethod
    def build_parser(self):
        """Retourne une instance du parser DOM du service."""

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        """Donnees non-DOM (ex: timestamps reactFiber ChatGPT).

        Defaut : le titre de la sidebar suffit comme indice pour le parser.
        """
        return {"title_hint": ref.title} if ref.title else {}

    # -- pipeline commun ----------------------------------------------------------

    def _scroll_config(self) -> Dict[str, int]:
        cfg = self.config.get("sidebar", {})
        return {
            "max_rounds": int(cfg.get("max_rounds", 25)),
            "pause_ms": int(cfg.get("pause_ms", 500)),
            "stable_rounds": int(cfg.get("stable_rounds", 2)),
        }

    def after_sidebar_open(self) -> None:
        """Hook apres ouverture de la home (ex: cliquer 'Show all' sur Gemini)."""

    def discover_via_api(self) -> Optional[List[ConversationRef]]:
        """Decouverte par API du service (surcharge). None = fallback DOM."""
        return None

    def _is_rate_limited(self) -> bool:
        """True si la page affiche un dialog de rate limite."""
        try:
            text = (self.session.evaluate(
                "return document.body ? document.body.innerText : '';"
            ) or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return any(marker in text for marker in RATE_LIMIT_MARKERS)

    def _wait_out_rate_limit(self, waits_s: Sequence[int] = (15, 30, 60)) -> bool:
        """Ferme le dialog et attend que la limite se leve. True si limite vue."""
        limited = False
        for wait_s in waits_s:
            if not self._is_rate_limited():
                return limited
            limited = True
            log_fields(
                log, 30, f"{self.name}: rate limite detecte -> attente {wait_s}s",
            )
            for sel in RATE_LIMIT_DISMISS_SELECTORS:
                if self.session.click_if_present(sel, timeout_ms=1500):
                    break
            self.session.wait_ms(wait_s * 1000)
        return limited

    def _screenshot(self, name: str) -> Optional[Path]:
        """Capture de debug optionnelle (desactivee si screenshot_dir est vide)."""
        directory = self.config.get("screenshot_dir")
        if not directory:
            return None
        try:
            return self.session.screenshot(Path(directory) / name)
        except Exception:  # noqa: BLE001
            return None

    def _assert_not_blocked(self, where: str) -> None:
        """Détecte un challenge anti-bot, tente un auto-click, puis leve BlockedError."""
        marker = self.session.looks_blocked()
        if not marker:
            return
        log_fields(log, 30, f"{self.name}: challenge anti-bot ({marker}) sur {where}, tentative de clic...",
                   extra={"service": self.name, "marker": marker})
        if self.session.try_solve_cloudflare():
            log_fields(log, 20, f"{self.name}: challenge resolu automatiquement",
                       extra={"service": self.name})
            return
        # certaines pages se resorbent apres un rechargement
        self.session.reload()
        self.session.wait_ms(2500)
        marker = self.session.looks_blocked()
        if not marker:
            return
        shot = self._screenshot(f"blocked_{self.name}.png")
        raise BlockedError(
            f"{self.name}: bloque par un challenge anti-bot ({marker}) sur {where}. "
            f"Solution: lancer une fois `python run.py --login {self.name}` (mode "
            f"visible) et cocher la case Cloudflare ; le cookie est alors "
            f"reutilise en headless." + (f" [capture: {shot}]" if shot else "")
        )

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        """Ouvre la home, scroll la sidebar, extrait les refs via le parser.

        Le lazy-load des sidebars est capricieux (liste partielle/vide) :
        si 0 ref est extraite, on recharge et retente (jusqu'a 3 essais).
        """
        refs = self._list_once(limit=limit)
        for wait_s in (3, 30, 60):
            if refs:
                break
            if self._is_rate_limited():
                # "Too many requests" : attendre la levee de la limite
                # (recharger tout de suite ne sert a rien)
                self._wait_out_rate_limit()
            log_fields(
                log, 30,
                f"{self.name}: 0 conversation -> rechargement + nouvelle tentative "
                f"(attente {wait_s}s)",
            )
            self.session.reload()
            self.session.wait_ms(wait_s * 1000)
            refs = self._list_once(limit=limit)
        if limit:
            refs = refs[:limit]
        return refs

    def _list_once(self, limit: Optional[int] = None) -> List[ConversationRef]:
        discover_url = self.discover_url or self.home_url
        self.session.goto(discover_url)
        self._assert_not_blocked("home")
        if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
            raise ServiceNotLoggedIn(
                f"{self.name}: pas de session valide -> lancer une execution "
                f"headless=false et se connecter sur {self.home_url}"
            )
        self.session.wait_for_any(list(self.sidebar_ready_selectors) or list(self.parser.link_selectors))
        self.after_sidebar_open()
        api_refs = self.discover_via_api()
        if api_refs:
            refs = {}
            for ref in api_refs:
                refs[ref.id] = ref
        else:
            refs = self._collect_sidebar_refs()
        if not refs:
            # le challenge Cloudflare peut apparaitre apres le goto : on
            # re-verifie avant de conclure a un DOM vide
            self._assert_not_blocked("home (post-listing)")
            shot = self._screenshot(f"debug_{self.name}_home.png")
            log_fields(
                log,
                30,
                f"{self.name}: 0 conversation detectee (session expirree ? DOM "
                f"change ? capture: {shot})",
                extra={"count": 0, "home_url": self.home_url},
            )
        log_fields(
            log,
            20,
            f"{self.name}: conversations trouvees",
            extra={"count": len(refs), "limit": limit},
        )
        return list(refs.values())

    # -- collecte incrementale de la liste (listes virtualisees) ----------------

    def _link_selector_css(self) -> str:
        """Selecteurs CSS purs des liens de conversation (sans pseudo Playwright)."""
        parts = []
        for raw in self.parser.link_selectors:
            sel = translate_selector(raw)
            if sel.plain and sel.css:
                parts.append(sel.css)
        return ", ".join(parts) if parts else "a[href]"

    def _scroll_list_js(self) -> str:
        """JS sautant au bas de tous les conteneurs scrollables.

        Les listes virtualisees (Perplexity) rendent les items dans un overlay
        sibling du vrai scroller, et Gemini utilise des composants custom
        (infinite-scroller) : impossible d'identifier le bon conteneur par
        tag ou par liens -> on scrolle tout element scrollable.
        """
        return (
            "let jumped = 0;"
            "for (const e of document.querySelectorAll('*')) {"
            "  if (e.scrollHeight <= e.clientHeight + 50 || e.clientHeight < 150) continue;"
            "  e.scrollTop = e.scrollHeight;"
            "  jumped++;"
            "}"
            "window.scrollTo(0, document.body.scrollHeight);"
            "return jumped;"
        )

    def _collect_refs_js(self) -> str:
        sel = json.dumps(self._link_selector_css())
        return (
            "return Array.from(document.querySelectorAll(" + sel + "))"
            ".map(a => [a.getAttribute('href') || '', (a.textContent || '').trim()]);"
        )

    def _collect_sidebar_refs(self) -> Dict[str, ConversationRef]:
        """Scrolle la liste en accumulant les refs (listes virtualisees : les
        elements hors viewport disparaissent du DOM, il faut collecter a
        chaque tour de scroll)."""
        cfg = self._scroll_config()
        refs: Dict[str, ConversationRef] = {}
        pattern = self.parser.conversation_id_pattern
        last_total = -1
        stable = 0
        max_rounds = max(30, cfg["max_rounds"])
        for _ in range(max_rounds):
            self.session.eval_body(self._scroll_list_js())
            self.session.wait_ms(cfg["pause_ms"])
            rows = self.session.eval_body(self._collect_refs_js()) or []
            for href, text in rows:
                match = pattern.search(href or "")
                if not match:
                    continue
                cid = match.group(1)
                if cid not in refs:
                    refs[cid] = ConversationRef(
                        service=self.parser.service_name,
                        id=cid,
                        url=urljoin(self.home_url, href),
                        title=text[:120] or None,
                    )
            if len(refs) == last_total:
                stable += 1
            else:
                stable = 0
            last_total = len(refs)
            if refs and stable >= max(2, cfg["stable_rounds"]):
                break
        return refs

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge une conversation, scroll jusqu'au bout, capture HTML + extras."""
        url = self.conversation_url(ref)
        self.session.goto(url)
        self._assert_not_blocked(f"conversation {ref.id}")
        if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
            raise ServiceNotLoggedIn(f"{self.name}: session perdue sur {url}")
        matched = self.session.wait_for_any(list(self.parser.message_selectors))
        if matched is None:
            # seconde chance : SPA lente / rate limite / rendu differe
            if self._is_rate_limited():
                self._wait_out_rate_limit()
            log_fields(
                log, 30,
                f"{self.name}: aucun message vu sur {url}, rechargement...",
            )
            self.session.reload()
            self.session.wait_ms(3000)
            # budget double : les fils volumineux rendent lentement
            retry_budget = int(getattr(self.session, "timeout_ms", 45000)) * 2
            matched = self.session.wait_for_any(
                list(self.parser.message_selectors), timeout_ms=retry_budget
            )
        if matched is None:
            for sel in self.empty_chat_selectors:
                if self.session.is_element_present(sel):
                    raise EmptyConversationError(
                        f"{self.name}: conversation sans message chargeable sur {url} "
                        f"(vide, tache, ou echec de rendu de l'app)"
                    )
            shot = self._screenshot(f"debug_{self.name}_conv.png")
            raise ParseError(
                f"{self.name}: aucun message reconnu sur {url} "
                f"(DOM change ? selecteurs: {self.parser.message_selectors})"
                + (f" [capture: {shot}]" if shot else "")
            )
        scroll_cfg = self.config.get("scroll", {})
        self.session.scroll_page_until_stable(
            max_rounds=int(scroll_cfg.get("max_rounds", 60)),
            pause_ms=int(scroll_cfg.get("pause_ms", 700)),
            stable_rounds=int(scroll_cfg.get("stable_rounds", 3)),
        )
        return ScrapedPage(
            html=self.session.html(),
            conversation_id=ref.id,
            url=url,
            extra=self.extract_extras(ref),
        )

    def parse_page(self, page: ScrapedPage, ref: ConversationRef) -> Conversation:
        conv = self.parser.parse(
            page.html, conversation_id=page.conversation_id, extra=page.extra
        )
        conv.platform = self.name
        if not conv.conversation_id:
            conv.conversation_id = ref.id
        return conv

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        """Retourne (Conversation, html complet de la page).

        `html` vaut None pour les services API (pas de page recuperable) : le
        payload brut du dernier export est alors fourni par `take_raw_payload`.
        """
        page = self.scrape_conversation(ref)
        return self.parse_page(page, ref), page.html

    def take_raw_payload(self) -> Optional[Any]:
        """Payload API brut du dernier export (None pour les services DOM).

        Les services API (ChatGPT, Claude, Gemini, Grok, Mistral, Perplexity)
        le memorisent pendant `export_conversation_with_html` ; l'orchestrateur
        l'archive alors dans `exports/<platform>/raw/<slug>.json`, l'equivalent
        du HTML brut. La methode consomme le payload : un export ulterieur ne
        le reecrit pas.
        """
        payload, self._raw_payload = self._raw_payload, None
        return payload

    def capture_message_screenshots(
        self, conversation: Conversation, out_dir: Path
    ) -> int:
        """Capture un screenshot par tour DOM (tour centre dans le viewport).

        Les fils virtualises ne montent qu'une fenetre de tours : on descend de
        maniere monotone (identite stable par tour) en defilant pour charger la
        suite, au lieu d'indexer la fenetre courante.

        Retourne le nombre de captures reussies. Sans selecteurs ou sans
        navigateur (Grok) : ne fait rien. En mode API (aucun DOM rendu), la
        garde `api_enabled` court-circuite tout.
        """
        if getattr(self, "api_enabled", False):
            return 0
        selectors = list(getattr(self.parser, "message_selectors", ()) or ())
        session = self.session
        if not selectors or not hasattr(session, "eval_body") or not hasattr(
            session, "screenshot"
        ):
            return 0
        if not session.eval_body(screenshot_reset_body(selectors)):
            return 0
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        session.wait_ms(SETTLE_MS)
        count = 0
        stable = 0
        seen_hashes: set = set()
        for _ in range(MAX_STEPS):
            step = session.eval_body(screenshot_step_body(selectors))
            if step == "shot":
                session.wait_ms(SETTLE_MS)
                tmp = out_dir / f".aicv-tmp-{count + 1:02d}.png"
                shot = session.screenshot(tmp)
                if shot:
                    digest = _file_digest(Path(shot))
                    if digest in seen_hashes:
                        # tour non amenageable (panneau artefact, virtualisation) :
                        # une image identique a la precedente induirait en erreur.
                        Path(shot).unlink(missing_ok=True)
                    else:
                        seen_hashes.add(digest)
                        Path(shot).replace(out_dir / f"message-{count + 1:02d}.png")
                        count += 1
                stable = 0
                if count >= MAX_TURNS:
                    break
            elif step == "advance":
                session.wait_ms(SETTLE_MS)
                stable += 1
                if stable >= STABLE_ROUNDS:
                    break
            else:
                break
        return count
