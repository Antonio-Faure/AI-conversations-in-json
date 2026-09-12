"""Runner resumable d'une conversation d'etalonnage.

Envoie, une par une, les entrees d'une file (`queue.json`) dans un chatbot via
un `ChatDriver`, en tenant un etat machine (`state.json`) et un bilan humain
(`BILAN.md`). Concu pour s'arreter proprement sur rate-limit et reprendre plus
tard sans renvoyer deux fois le meme message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.file_utils import now_iso_z, write_json_atomic
from ..utils.logging import get_logger, log_fields

log = get_logger("aicv.etalon")

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_ERROR = "error"


@dataclass
class RunnerState:
    bot: str
    run_dir: str
    target_url: Optional[str] = None
    next_index: int = 0
    sent: List[Dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_RUNNING
    started_at: str = ""
    updated_at: str = ""
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bot": self.bot,
            "run_dir": self.run_dir,
            "target_url": self.target_url,
            "next_index": self.next_index,
            "sent": self.sent,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }


def load_queue(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        raise ValueError(f"queue invalide (attendu une liste de messages): {path}")
    return [m if isinstance(m, dict) else {"text": str(m)} for m in messages]


class EtalonRunner:
    """Envoie la file de messages via un driver, jusqu'au rate-limit."""

    def __init__(
        self,
        driver,
        run_dir: Path,
        messages: List[Dict[str, Any]],
        state: Optional[RunnerState] = None,
    ):
        self.driver = driver
        self.run_dir = Path(run_dir)
        self.messages = messages
        self.state = state or RunnerState(
            bot=getattr(driver, "name", "?"), run_dir=str(self.run_dir)
        )
        self.state.bot = self.state.bot or getattr(driver, "name", "?")
        self.state.run_dir = str(self.run_dir)

    # -- etat ------------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def bilan_path(self) -> Path:
        return self.run_dir / "BILAN.md"

    def save_state(self) -> None:
        self.state.updated_at = now_iso_z()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.state_path, self.state.to_dict())

    def write_bilan(self, reason: str) -> None:
        total = len(self.messages)
        remaining = max(0, total - self.state.next_index)
        lines = [
            f"# BILAN etalonnage — {self.state.bot}",
            "",
            f"- Statut : **{self.state.status}** ({reason})",
            f"- Progression : {self.state.next_index}/{total} messages envoyes",
            f"- Restants : {remaining}",
            f"- Conversation cible : {self.state.target_url or '(non ouverte)'}",
            f"- Derniere erreur : {self.state.last_error or '(aucune)'}",
            f"- Mis a jour : {self.state.updated_at or now_iso_z()}",
            "",
            "## Reprise",
            "",
            "Relancer le runner sur ce dossier : il reprend a `next_index` "
            "(`state.json`) sans renvoyer les messages deja partis.",
            "",
            "## Messages restants",
            "",
        ]
        for index in range(self.state.next_index, total):
            item = self.messages[index]
            attachments = item.get("attachments") or []
            suffix = f" [fichiers: {', '.join(attachments)}]" if attachments else ""
            text = (item.get("text") or "").strip().replace("\n", " ")
            lines.append(f"{index + 1}. {text[:160]}{suffix}")
        if remaining == 0:
            lines.append("(aucun)")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.bilan_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- execution -------------------------------------------------------------

    def _refresh_target_url(self) -> None:
        """Recapture l'URL apres envoi (un nouveau chat n'obtient son id qu'apres
        le 1er message : au depart l'URL peut etre `/` ou `/new`)."""
        try:
            url = self.driver.session.url()
        except Exception:  # noqa: BLE001
            return
        if url and url != self.state.target_url:
            self.state.target_url = url

    def _open_target(self) -> bool:
        if self.state.target_url:
            self.driver.open_conversation(self.state.target_url)
            return True
        if not self.driver.new_conversation():
            self.state.last_error = "impossible d'ouvrir une nouvelle conversation"
            return False
        try:
            self.state.target_url = self.driver.session.url()
        except Exception:  # noqa: BLE001
            self.state.target_url = None
        return True

    def run(self) -> str:
        if self.state.status == STATUS_DONE:
            return self.state.status
        if not self.state.started_at:
            self.state.started_at = now_iso_z()
        self.state.status = STATUS_RUNNING
        self.save_state()

        if not self._open_target():
            self.state.status = STATUS_ERROR
            self.save_state()
            self.write_bilan("ouverture impossible")
            return self.state.status

        total = len(self.messages)
        try:
            while self.state.next_index < total:
                index = self.state.next_index
                if self.driver.is_rate_limited():
                    self.state.status = STATUS_RATE_LIMITED
                    break
                item = self.messages[index]
                attachments = [
                    self.run_dir / "attachments" / name
                    for name in (item.get("attachments") or [])
                ]
                if attachments and not self.driver.attach(attachments):
                    self.state.last_error = f"upload echoue (message {index + 1})"
                    self.state.status = STATUS_ERROR
                    break
                if not self.driver.send(item.get("text") or ""):
                    self.state.last_error = f"envoi echoue (message {index + 1})"
                    self.state.status = STATUS_ERROR
                    break
                self.driver.wait_for_response()
                self._refresh_target_url()
                self.state.next_index = index + 1
                self.state.sent.append(
                    {"index": index, "at": now_iso_z(), "attachments": [
                        Path(a).name for a in attachments
                    ]}
                )
                self.state.last_error = None
                self.save_state()
                log_fields(
                    log, 20, f"{self.state.bot}: {self.state.next_index}/{total}",
                    extra={"bot": self.state.bot},
                )
                self.driver.pause()
                if self.driver.is_rate_limited():
                    self.state.status = STATUS_RATE_LIMITED
                    break
            else:
                self.state.status = STATUS_DONE
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
            self.state.status = STATUS_ERROR
            log.exception("etalon: erreur inattendue")

        self.save_state()
        reasons = {
            STATUS_DONE: "tous les messages envoyes",
            STATUS_RATE_LIMITED: "rate-limit atteint, reprise plus tard",
            STATUS_ERROR: "erreur, voir last_error",
        }
        self.write_bilan(reasons.get(self.state.status, self.state.status))
        return self.state.status


def load_state(path: Path, bot: str, run_dir: Path) -> RunnerState:
    if not Path(path).exists():
        return RunnerState(bot=bot, run_dir=str(run_dir))
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunnerState(
        bot=data.get("bot") or bot,
        run_dir=data.get("run_dir") or str(run_dir),
        target_url=data.get("target_url"),
        next_index=int(data.get("next_index") or 0),
        sent=list(data.get("sent") or []),
        status=data.get("status") or STATUS_RUNNING,
        started_at=data.get("started_at") or "",
        updated_at=data.get("updated_at") or "",
        last_error=data.get("last_error"),
    )
