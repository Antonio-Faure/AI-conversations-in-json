#!/usr/bin/env python3
"""Serveur MCP (stdio) : recherche dans les conversations IA archivees.

Outils exposes (protocole MCP, transport stdio) :
  - search             : recherche hybride (embeddings BGE-M3 + mots-cles)
  - grep               : recherche litterale/regex dans les exports JSON
  - get_conversation   : JSON standardise complet (ou resume + N messages)
  - get_message        : un message precis (message_id ou index 1-based)
  - list_conversations : inventaire depuis conversation_list.json
  - stats              : compteurs par plateforme (index RAG + exports)

Le modele d'embeddings n'est charge qu'au premier `search` (paresseux). stdout
est reserve au protocole JSON-RPC : logs et erreurs vont sur stderr.

    .venv/bin/python scripts/mcp_conversations.py [--config config.yaml]

Le serveur s'enregistre cote client opencode sous le nom `aicv` (config
globale, section `mcp.servers`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import PATH_KEYS, load_config  # noqa: E402
from src.rag.search import combined_search  # noqa: E402
from src.schema import parse_iso, to_iso_z  # noqa: E402
from src.utils.file_utils import (  # noqa: E402
    CONVERSATION_LIST_NAME,
    RESERVED_JSON_NAMES,
    load_conversation_list,
)
from src.utils.urls import conversation_url  # noqa: E402

SERVER_NAME = "aicv"
SERVER_VERSION = "1.0.0"

#: bornes de securite des parametres (evitent les reponses geantes)
MAX_K = 100
MAX_GREP_LIMIT = 500
MAX_LIST_LIMIT = 1000
SNIPPET_WIDTH = 400
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_WS_RE = re.compile(r"\s+")


class ConversationToolError(ValueError):
    """Erreur attendue d'un outil : message clair renvoye au client MCP."""


# -- config ----------------------------------------------------------------------


def load_server_config(path: Optional[Path | str]) -> Dict[str, Any]:
    """Config du depot, chemins resolus depuis le dossier du fichier de config.

    `load_config` resout les chemins relatifs depuis la racine du depot (la ou
    vit `src/`) ; ici le serveur peut etre pointe vers un config.yaml externe
    (worktree), donc on reprend les valeurs brutes du fichier et on les resout
    depuis SON dossier. Les valeurs par defaut/absolues restent inchangees.
    Un fichier explicitement demande mais absent leve une erreur claire (un
    serveur lance avec un mauvais chemin ne doit pas lire silencieusement les
    exports par defaut).
    """
    if path is None:
        return load_config(path)
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"fichier de configuration introuvable: {config_path}")
    config = load_config(config_path)
    base = config_path.resolve().parent
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return config
    if not isinstance(raw, dict):
        return config
    for key in PATH_KEYS:
        value = raw.get(key)
        if not value:
            continue
        resolved = Path(str(value)).expanduser()
        config[key] = str(resolved if resolved.is_absolute() else base / resolved)
    return config


# -- helpers de texte -------------------------------------------------------------


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def _snippet(text: str, needles: Sequence[str] = (), width: int = SNIPPET_WIDTH) -> str:
    """Texte compact : fenetre autour du premier terme trouve, sinon debut."""
    flat = _collapse(text)
    if len(flat) <= width:
        return flat
    low = flat.lower()
    index = -1
    for needle in needles:
        if not needle:
            continue
        found = low.find(needle.lower())
        if found >= 0 and (index < 0 or found < index):
            index = found
    start = max(0, index - width // 3) if index >= 0 else 0
    end = min(len(flat), start + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


def _window(text: str, start: int, end: int, before: int = 120, after: int = 180) -> str:
    """Extrait autour d'une position (pour grep, sans recopier tout le champ)."""
    lo = max(0, start - before)
    hi = min(len(text), end + after)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{_collapse(text[lo:hi])}{suffix}"


def _normalize_date(value: Any, end_of_day: bool = False) -> Optional[str]:
    """Date ISO-8601 -> ISO Z. Une date seule devient debut/fin de journee."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    parsed = parse_iso(text)
    if parsed is None:
        raise ConversationToolError(
            f"date invalide {value!r} (attendu ISO-8601, ex. 2026-06-08)"
        )
    if end_of_day and DATE_RE.fullmatch(text):
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return to_iso_z(parsed)


def _find_literal(text: str, pattern: str, limit: int) -> List[Tuple[int, int]]:
    """Occurrences de `pattern` (insensible a la casse), bornees a `limit`."""
    found: List[Tuple[int, int]] = []
    low = text.lower()
    needle = pattern.lower()
    start = low.find(needle)
    while start >= 0 and len(found) < limit:
        found.append((start, start + len(needle)))
        start = low.find(needle, start + len(needle))
    return found


def _find_regex(text: str, pattern: "re.Pattern[str]", limit: int) -> List[Tuple[int, int]]:
    found: List[Tuple[int, int]] = []
    for match in pattern.finditer(text):
        if len(found) >= limit:
            break
        if match.end() == match.start():  # motif vide : eviter les boucles inutiles
            continue
        found.append((match.start(), match.end()))
    return found


def _message_fields(message: Dict[str, Any]) -> Iterable[Tuple[str, str]]:
    """Champs textuels d'un message ou chercher une chaine (hors texte principal)."""
    for field in ("texte", "reasoning", "message_id", "model", "timestamp"):
        value = message.get(field)
        if isinstance(value, str) and value:
            yield field, value
    for field in ("sources", "tools", "artifacts"):
        for index, value in enumerate(message.get(field) or []):
            if value:
                yield f"{field}[{index}]", str(value)
    for index, variant in enumerate(message.get("variants") or []):
        text = (variant or {}).get("texte")
        if text:
            yield f"variants[{index}].texte", str(text)


# -- outils ------------------------------------------------------------------------


class ConversationTools:
    """Handlers des outils MCP (utilisables/testables sans serveur ni protocole)."""

    def __init__(
        self,
        config: Dict[str, Any],
        store: Any = None,
        embedder: Any = None,
    ):
        self.exports_dir = Path(config["output_dir"])
        self.db_path = Path(config["rag_db"])
        self.model_name = config.get("rag_model") or "BAAI/bge-m3"
        self._store = store
        self._embedder = embedder
        # l'index sqlite et le modele ne supportent pas l'acces concurrent
        self._lock = threading.Lock()

    # -- ressources paresseuses ------------------------------------------------

    def store(self):
        """VectorStore (sqlite-vec) ; erreur claire si l'index n'existe pas."""
        if self._store is None:
            if not self.db_path.exists():
                raise ConversationToolError(
                    f"index RAG introuvable: {self.db_path} "
                    "(lancez `.venv/bin/python scripts/rag_index.py`)"
                )
            from src.rag.store import VectorStore

            self._store = VectorStore(self.db_path)
        return self._store

    def embedder(self):
        """Embedder BGE-M3, charge au premier usage (import torch differe)."""
        if self._embedder is None:
            from src.rag.embeddings import Embedder

            self._embedder = Embedder(self.model_name)
        return self._embedder

    # -- inventaire -------------------------------------------------------------

    def platforms(self) -> List[str]:
        if not self.exports_dir.is_dir():
            return []
        return sorted(
            d.name
            for d in self.exports_dir.iterdir()
            if d.is_dir() and (d / CONVERSATION_LIST_NAME).exists()
        )

    def _check_platform(self, platform: Optional[str]) -> Optional[str]:
        if not platform:
            return None
        available = self.platforms()
        if platform not in available:
            raise ConversationToolError(
                f"plateforme inconnue {platform!r} "
                f"(disponibles: {', '.join(available) or 'aucune'})"
            )
        return platform

    def _list_index(self, platform: str) -> Dict[str, Dict[str, Any]]:
        return load_conversation_list(
            self.exports_dir / platform / CONVERSATION_LIST_NAME
        )

    def _conversation_files(self, platform: Optional[str] = None):
        for name in self.platforms():
            if platform and name != platform:
                continue
            for path in sorted((self.exports_dir / name).glob("*.json")):
                if path.name == CONVERSATION_LIST_NAME:
                    continue
                yield name, path

    def _read_conversation_file(self, path: Path) -> Dict[str, Any]:
        """Charge un JSON de conversation ; erreur claire si illisible."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConversationToolError(f"fichier illisible: {path} ({exc})") from exc
        if not isinstance(data, dict):
            raise ConversationToolError(f"fichier invalide (objet JSON attendu): {path}")
        return data

    def _load_conversation(
        self, platform: str, conversation_id: str
    ) -> Tuple[Dict[str, Any], Path]:
        """Retrouve le fichier JSON d'une conversation (id exact ou prefixe)."""
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            raise ConversationToolError("conversation_id vide")
        platform_dir = self.exports_dir / platform

        # 1) conversation_list.json : chemin direct par id exact ou prefixe
        index = self._list_index(platform)
        if conversation_id in index:
            entry = index[conversation_id]
            if entry.get("file"):
                candidate = platform_dir / f"{entry['file']}.json"
                if candidate.exists():
                    return self._read_conversation_file(candidate), candidate
            if not entry.get("scraped"):
                raise ConversationToolError(
                    f"conversation {conversation_id} non scrapee sur {platform} "
                    "(presente dans conversation_list.json sans fichier)"
                )
        prefix_entries = [
            (cid, entry)
            for cid, entry in index.items()
            if cid.startswith(conversation_id)
        ]
        if len(prefix_entries) == 1:
            cid, entry = prefix_entries[0]
            if entry.get("file"):
                candidate = platform_dir / f"{entry['file']}.json"
                if candidate.exists():
                    return self._read_conversation_file(candidate), candidate
        elif len(prefix_entries) > 1:
            ids = ", ".join(sorted(cid for cid, _ in prefix_entries)[:5])
            raise ConversationToolError(
                f"prefixe {conversation_id!r} ambigu sur {platform} : {ids}…"
            )

        # 2) repli : scan des fichiers (id exact puis prefixe unique)
        prefix_matches: List[Tuple[str, Path]] = []
        for name, path in self._conversation_files(platform):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            cid = str(data.get("conversation_id") or "")
            if cid == conversation_id:
                return data, path
            if cid.startswith(conversation_id):
                prefix_matches.append((cid, path))
        if len(prefix_matches) == 1:
            data = self._read_conversation_file(prefix_matches[0][1])
            return data, prefix_matches[0][1]
        if len(prefix_matches) > 1:
            ids = ", ".join(sorted(cid for cid, _ in prefix_matches)[:5])
            raise ConversationToolError(
                f"prefixe {conversation_id!r} ambigu sur {platform} : {ids}…"
            )
        raise ConversationToolError(
            f"conversation {conversation_id!r} introuvable sur {platform}"
        )

    # -- outil : search ----------------------------------------------------------

    def search(
        self,
        query: str,
        k: int = 5,
        platforms: Optional[List[str]] = None,
        alpha: float = 0.6,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Recherche hybride (cosinus BGE-M3 + mots-cles) groupee par conversation."""
        query = (query or "").strip()
        if not query:
            raise ConversationToolError("query vide")
        k = max(1, min(int(k), MAX_K))
        alpha = max(0.0, min(1.0, float(alpha)))
        selected = [p for p in (platforms or []) if p]
        for platform in selected:
            self._check_platform(platform)
        selected = selected or None
        start = _normalize_date(date_from)
        end = _normalize_date(date_to, end_of_day=True)

        with self._lock:
            store = self.store()
            vector = self.embedder().encode_one(query)
            results = combined_search(
                store,
                vector,
                query,
                k=k,
                alpha=alpha,
                platforms=selected,
                model=model or None,
                date_from=start,
                date_to=end,
            )
            groups = self._group_results(store, results, query)
        return {
            "query": query,
            "k": k,
            "alpha": alpha,
            "filters": {
                "platforms": selected,
                "model": model or None,
                "date_from": start,
                "date_to": end,
            },
            "count": len(results),
            "group_count": len(groups),
            "groups": groups,
        }

    def _group_results(
        self, store, results: List[Dict[str, Any]], query: str
    ) -> List[Dict[str, Any]]:
        """Regroupe les messages par conversation (multi-hits d'abord)."""
        terms = [t for t in re.split(r"\s+", query) if t]
        groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for item in results:
            key = (item["platform"], item["conversation_id"])
            group = groups.get(key)
            if group is None:
                group = {
                    "platform": item["platform"],
                    "conversation_id": item["conversation_id"],
                    "title": "",
                    "url": conversation_url(item["platform"], item["conversation_id"]),
                    "best_score": 0.0,
                    "hit_count": 0,
                    "messages": [],
                }
                groups[key] = group
            group["messages"].append(
                {
                    "index": self._message_index(
                        store, item["platform"], item["conversation_id"], item.get("rowid")
                    ),
                    "message_id": item["message_id"],
                    "role": item["role"],
                    "timestamp": item["timestamp"],
                    "score": item["score"],
                    "semantic_score": item["semantic_score"],
                    "keyword_score": item["keyword_score"],
                    "snippet": _snippet(item["texte"], terms),
                }
            )
            group["hit_count"] += 1
            group["best_score"] = max(group["best_score"], item["score"])
        meta = store.conversation_meta(list(groups.keys()))
        for key, group in groups.items():
            info = meta.get(key) or {}
            group["title"] = info.get("title") or ""
            if info.get("url"):
                group["url"] = info["url"]
            group["messages"].sort(key=lambda m: m["score"], reverse=True)
            group["best_score"] = round(group["best_score"], 4)
        return sorted(
            groups.values(),
            key=lambda g: (0 if g["hit_count"] >= 2 else 1, -g["best_score"]),
        )

    @staticmethod
    def _message_index(store, platform: str, conversation_id: str, rowid) -> Optional[int]:
        """Index 1-based du message dans sa conversation (via l'ordre des rowid)."""
        if rowid is None:
            return None
        row = store.db.execute(
            """
            SELECT COUNT(*) AS n FROM messages
            WHERE platform=? AND conversation_id=? AND rowid < ?
            """,
            (platform, conversation_id, int(rowid)),
        ).fetchone()
        return int(row["n"]) + 1

    # -- outil : grep --------------------------------------------------------------

    def grep(
        self,
        pattern: str,
        platform: Optional[str] = None,
        regex: bool = False,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Recherche litterale (ou regex) dans les exports JSON, sans modele."""
        pattern = pattern or ""
        if not pattern:
            raise ConversationToolError("pattern vide")
        limit = max(1, min(int(limit), MAX_GREP_LIMIT))
        platform = self._check_platform(platform)
        compiled = None
        if regex:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ConversationToolError(f"regex invalide: {exc}") from exc

        matches: List[Dict[str, Any]] = []
        scanned = 0
        truncated = False
        for name, path in self._conversation_files(platform):
            if len(matches) >= limit:
                truncated = True
                break
            scanned += 1
            matches.extend(
                self._grep_file(name, path, pattern, compiled, limit - len(matches))
            )
        # conversations non scrapees : seule conversation_list.json les contient
        if len(matches) < limit:
            matches.extend(
                self._grep_lists(pattern, compiled, platform, limit - len(matches))
            )
        return {
            "pattern": pattern,
            "regex": bool(regex),
            "platform": platform,
            "count": len(matches),
            "truncated": truncated,
            "files_scanned": scanned,
            "matches": matches,
        }

    def _grep_file(
        self,
        platform: str,
        path: Path,
        pattern: str,
        compiled: Optional["re.Pattern[str]"],
        budget: int,
    ) -> List[Dict[str, Any]]:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        # pre-filtre rapide sur le brut ; un motif non-ASCII n'est PAS pre-filtre
        # (le brut peut contenir des echappements \uXXXX que le parseur decode)
        if compiled is not None:
            if not compiled.search(raw):
                return []
        elif pattern.isascii() and pattern.lower() not in raw.lower():
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []

        found: List[Dict[str, Any]] = []
        base = {
            "platform": platform,
            "conversation_id": str(data.get("conversation_id") or ""),
            "title": data.get("title") or "",
            "url": data.get("url")
            or conversation_url(platform, str(data.get("conversation_id") or "")),
            "file": path.name,
        }

        def collect(text: str, field: str, message: Optional[Dict[str, Any]] = None,
                    index: Optional[int] = None) -> None:
            if not text or len(found) >= budget:
                return
            spans = (
                _find_regex(text, compiled, budget - len(found))
                if compiled is not None
                else _find_literal(text, pattern, budget - len(found))
            )
            for start, end in spans:
                item = dict(base)
                item.update({"field": field, "snippet": _window(text, start, end)})
                if message is not None:
                    item.update(
                        {
                            "message_index": index,
                            "message_id": message.get("message_id") or "",
                            "role": message.get("role") or "",
                        }
                    )
                found.append(item)
                if len(found) >= budget:
                    return

        for field in ("title", "url", "conversation_id", "model"):
            collect(str(data.get(field) or ""), field)
        for index, message in enumerate(data.get("messages") or [], 1):
            if not isinstance(message, dict):
                continue
            for field, text in _message_fields(message):
                collect(text, field, message, index)
                if len(found) >= budget:
                    break
            if len(found) >= budget:
                break
        if not found:
            # le brut contient le motif sans qu'aucun champ texte ne le porte
            # (champ interne, structure inattendue)
            span = compiled.search(raw) if compiled is not None else None
            if span is not None:
                start, end = span.start(), span.end()
            else:
                position = raw.lower().find(pattern.lower())
                if position < 0:
                    return []
                start, end = position, position + len(pattern)
            item = dict(base)
            item.update({"field": "raw", "snippet": _window(raw, start, end)})
            found.append(item)
        return found

    def _grep_lists(
        self,
        pattern: str,
        compiled: Optional["re.Pattern[str]"],
        platform: Optional[str],
        budget: int,
    ) -> List[Dict[str, Any]]:
        """Titres/URLs de conversation_list.json (conversations non scrapees)."""
        found: List[Dict[str, Any]] = []
        for name in self.platforms():
            if platform and name != platform:
                continue
            for cid, entry in self._list_index(name).items():
                if len(found) >= budget:
                    return found
                if entry.get("scraped"):
                    continue  # deja couvert par le fichier de conversation
                for field in ("title", "url", "conversation_id"):
                    text = str(entry.get(field) or "")
                    if not text:
                        continue
                    spans = (
                        _find_regex(text, compiled, budget - len(found))
                        if compiled is not None
                        else _find_literal(text, pattern, budget - len(found))
                    )
                    for start, end in spans:
                        found.append(
                            {
                                "platform": name,
                                "conversation_id": str(cid),
                                "title": entry.get("title") or "",
                                "url": entry.get("url")
                                or conversation_url(name, str(cid)),
                                "field": f"list.{field}",
                                "unavailable": bool(entry.get("unavailable")),
                                "snippet": _window(text, start, end),
                            }
                        )
                        if len(found) >= budget:
                            return found
        return found

    # -- outil : get_conversation ----------------------------------------------------

    def get_conversation(
        self, platform: str, conversation_id: str, max_messages: int = 40
    ) -> Dict[str, Any]:
        """JSON standardise d'une conversation ; max_messages > 0 = N premiers."""
        platform = self._check_platform(platform)
        if not platform:
            raise ConversationToolError("platform requise")
        data, path = self._load_conversation(platform, conversation_id)
        messages = data.get("messages") or []
        max_messages = max(0, int(max_messages or 0))
        truncated = bool(max_messages and len(messages) > max_messages)
        payload = dict(data)
        if truncated:
            payload["messages"] = messages[:max_messages]
        return {
            "file": str(path),
            "total_messages": len(messages),
            "truncated": truncated,
            "conversation": payload,
        }

    # -- outil : get_message ----------------------------------------------------------

    def get_message(
        self, platform: str, conversation_id: str, message_id_or_index: Any
    ) -> Dict[str, Any]:
        """Un message par message_id (ou prefixe) ou par index 1-based."""
        platform = self._check_platform(platform)
        if not platform:
            raise ConversationToolError("platform requise")
        data, path = self._load_conversation(platform, conversation_id)
        messages = data.get("messages") or []
        target = str(message_id_or_index or "").strip()
        if not target:
            raise ConversationToolError("message_id_or_index vide")
        matched_by = "index"
        if target.isdigit():
            index = int(target)
            if not 1 <= index <= len(messages):
                raise ConversationToolError(
                    f"index {index} hors bornes (1..{len(messages)})"
                )
        else:
            matched_by = "message_id"
            exact = [
                i for i, m in enumerate(messages, 1)
                if str((m or {}).get("message_id") or "") == target
            ]
            if not exact:
                exact = [
                    i for i, m in enumerate(messages, 1)
                    if str((m or {}).get("message_id") or "").startswith(target)
                ]
            if not exact:
                raise ConversationToolError(
                    f"message_id {target!r} introuvable "
                    f"({len(messages)} messages dans la conversation)"
                )
            if len(exact) > 1:
                raise ConversationToolError(
                    f"message_id {target!r} ambigu ({len(exact)} correspondances)"
                )
            index = exact[0]
        return {
            "file": str(path),
            "platform": platform,
            "conversation_id": str(data.get("conversation_id") or ""),
            "title": data.get("title") or "",
            "url": data.get("url") or conversation_url(platform, str(data.get("conversation_id") or "")),
            "index": index,
            "total_messages": len(messages),
            "matched_by": matched_by,
            "message": messages[index - 1],
        }

    # -- outil : list_conversations ----------------------------------------------------

    def list_conversations(
        self, platform: Optional[str] = None, limit: int = 20, sort: str = "recent"
    ) -> Dict[str, Any]:
        """Inventaire conversation_list.json, trie (recent|title|messages)."""
        platform = self._check_platform(platform)
        limit = max(0, min(int(limit), MAX_LIST_LIMIT))
        sort = (sort or "recent").lower()
        if sort not in ("recent", "title", "messages"):
            raise ConversationToolError(
                f"tri inconnu {sort!r} (attendu: recent, title, messages)"
            )
        items: List[Dict[str, Any]] = []
        for name in self.platforms():
            if platform and name != platform:
                continue
            for cid, entry in self._list_index(name).items():
                items.append(
                    {
                        "platform": name,
                        "conversation_id": str(cid),
                        "title": entry.get("title") or "",
                        "url": entry.get("url")
                        or conversation_url(name, str(cid)),
                        "message_count": entry.get("message_count"),
                        "last_message_at": entry.get("last_message_at"),
                        "has_code": entry.get("has_code"),
                        "scraped": bool(entry.get("scraped")),
                        "unavailable": bool(entry.get("unavailable")),
                        "file": entry.get("file"),
                        "scraped_at": entry.get("scraped_at"),
                    }
                )
        key = {
            "recent": lambda e: (e["last_message_at"] or "", e["scraped_at"] or ""),
            "title": lambda e: (e["title"] or "").lower(),
            "messages": lambda e: (e["message_count"] or 0),
        }[sort]
        items.sort(key=key, reverse=sort != "title")
        shown = items if limit == 0 else items[:limit]
        return {
            "platform": platform,
            "sort": sort,
            "total": len(items),
            "count": len(shown),
            "truncated": len(shown) < len(items),
            "conversations": shown,
        }

    # -- outil : stats -----------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Compteurs par plateforme : index RAG et exports."""
        exports: Dict[str, Dict[str, Any]] = {}
        total_files = 0
        for name in self.platforms():
            index = self._list_index(name)
            files = [
                p
                for p in (self.exports_dir / name).glob("*.json")
                if p.name not in RESERVED_JSON_NAMES
            ]
            total_files += len(files)
            exports[name] = {
                "listed": len(index),
                "scraped": sum(1 for e in index.values() if e.get("scraped")),
                "unavailable": sum(1 for e in index.values() if e.get("unavailable")),
                "files": len(files),
            }
        payload: Dict[str, Any] = {
            "exports_dir": str(self.exports_dir),
            "rag_db": str(self.db_path),
            "rag_model": self.model_name,
            "exports": {
                "total_files": total_files,
                "total_listed": sum(v["listed"] for v in exports.values()),
                "per_platform": exports,
            },
        }
        if not self.db_path.exists():
            payload["rag"] = {
                "available": False,
                "error": (
                    f"index RAG introuvable: {self.db_path} "
                    "(lancez `.venv/bin/python scripts/rag_index.py`)"
                ),
            }
            return payload
        with self._lock:
            store = self.store()
            counters = store.stats()
            per_platform: Dict[str, Dict[str, Any]] = {
                platform: {"conversations": count}
                for platform, count in store.db.execute(
                    "SELECT platform, COUNT(*) AS n FROM conversations GROUP BY platform"
                ).fetchall()
            }
            for platform, count in store.db.execute(
                "SELECT platform, COUNT(*) AS n FROM messages GROUP BY platform"
            ).fetchall():
                per_platform.setdefault(platform, {})["messages"] = count
            indexed_at = store.db.execute(
                "SELECT MAX(indexed_at) AS value FROM conversations"
            ).fetchone()["value"]
        payload["rag"] = {
            "available": True,
            "messages": counters["messages"],
            "conversations": counters["conversations"],
            "indexed_at": indexed_at,
            "per_platform": per_platform,
            "size_bytes": self.db_path.stat().st_size,
        }
        return payload


# -- couche MCP ----------------------------------------------------------------------

SEARCH_DESCRIPTION = (
    "Recherche hybride (embeddings BGE-M3 + mots-cles) dans les messages des "
    "conversations IA archivees (chatgpt, claude, gemini, perplexity, grok, "
    "mistral). Resultats groupes par conversation : titre, url, id et messages "
    "matches avec extraits et scores. A utiliser pour une question en langage "
    "naturel ; pour une chaine exacte (URL, identifiant), preferer grep. "
    "Filtres optionnels : platforms, model, dates ISO (date_from/date_to, une "
    "date seule = journee entiere), alpha (0=mots-cles, 1=embeddings)."
)

GREP_DESCRIPTION = (
    "Recherche litterale (ou regex) rapide dans les exports JSON standardises, "
    "sans modele d'embeddings. Ideal pour une chaine exacte, une URL, un "
    "identifiant, une expression de code. Cherche dans les champs texte, "
    "raisonnement, sources, outils, artefacts et variantes des messages, puis "
    "dans conversation_list.json (conversations non scrapees). Insensible a la "
    "casse."
)

GET_CONVERSATION_DESCRIPTION = (
    "Renvoie le JSON standardise d'une conversation archivee (messages, "
    "metadonnees). conversation_id accepte un id complet ou un prefixe non "
    "ambigu. max_messages=40 par defaut (0 = tout) ; max_messages=N = les N premiers "
    "messages, avec total_messages et truncated."
)

GET_MESSAGE_DESCRIPTION = (
    "Renvoie un message precis d'une conversation : par index 1-based (ex. "
    "\"3\") ou par message_id (ou prefixe non ambigu). Le message contient "
    "texte, code_blocks, reasoning, sources, tools, artifacts et variants."
)

LIST_CONVERSATIONS_DESCRIPTION = (
    "Inventaire des conversations depuis conversation_list.json (titre, url, "
    "message_count, last_message_at, scraped, unavailable). Tri : recent "
    "(defaut), title, messages. limit=0 = tout."
)

STATS_DESCRIPTION = (
    "Compteurs par plateforme : index RAG (messages/conversations indexes, "
    "date d'indexation) et exports (conversations listees/scrapees/"
    "indisponibles). A utiliser pour verifier la fraicheur de l'index avant de "
    "conclure qu'une recherche ne donne rien."
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, default=str)


def build_server(tools: ConversationTools):
    """Serveur MCP (SDK officiel `mcp`) : 6 outils au-dessus de ConversationTools."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as exc:  # pragma: no cover - dependance declaree
        raise SystemExit(
            "SDK MCP absent : installer avec "
            "`.venv/bin/pip install 'mcp>=2'` (voir requirements.txt)"
        ) from exc

    server = MCPServer(
        name=SERVER_NAME,
        title="Conversations IA archivees",
        version=SERVER_VERSION,
        instructions=(
            "Recherche dans les conversations IA archivees localement (exports "
            "JSON + index RAG). Les chemins sont donnes par config.yaml."
        ),
        log_level="WARNING",
    )

    def call(handler, **kwargs) -> str:
        try:
            return _dump(handler(**kwargs))
        except ConversationToolError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(description=SEARCH_DESCRIPTION)
    def search(
        query: str,
        k: int = 5,
        platforms: Optional[List[str]] = None,
        alpha: float = 0.6,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        return call(
            tools.search,
            query=query,
            k=k,
            platforms=platforms,
            alpha=alpha,
            date_from=date_from,
            date_to=date_to,
            model=model,
        )

    @server.tool(description=GREP_DESCRIPTION)
    def grep(
        pattern: str,
        platform: Optional[str] = None,
        regex: bool = False,
        limit: int = 20,
    ) -> str:
        return call(tools.grep, pattern=pattern, platform=platform, regex=regex, limit=limit)

    @server.tool(description=GET_CONVERSATION_DESCRIPTION)
    def get_conversation(
        platform: str, conversation_id: str, max_messages: int = 40
    ) -> str:
        return call(
            tools.get_conversation,
            platform=platform,
            conversation_id=conversation_id,
            max_messages=max_messages,
        )

    @server.tool(description=GET_MESSAGE_DESCRIPTION)
    def get_message(
        platform: str, conversation_id: str, message_id_or_index: str
    ) -> str:
        return call(
            tools.get_message,
            platform=platform,
            conversation_id=conversation_id,
            message_id_or_index=message_id_or_index,
        )

    @server.tool(description=LIST_CONVERSATIONS_DESCRIPTION)
    def list_conversations(
        platform: Optional[str] = None, limit: int = 20, sort: str = "recent"
    ) -> str:
        return call(
            tools.list_conversations, platform=platform, limit=limit, sort=sort
        )

    @server.tool(description=STATS_DESCRIPTION)
    def stats() -> str:
        return call(tools.stats)

    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=ROOT / "config.yaml",
        help="fichier de configuration (defaut: <racine>/config.yaml)",
    )
    args = parser.parse_args(argv)
    try:
        config = load_server_config(args.config)
    except FileNotFoundError as exc:
        print(f"erreur: {exc}", file=sys.stderr)
        return 2
    tools = ConversationTools(config)
    build_server(tools).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
