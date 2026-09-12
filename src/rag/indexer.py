"""Indexation incrementale des exports dans le store vectoriel.

Un vecteur par message (texte complet, code inclus). Une conversation dont
`exported_at` n'a pas change est ignoree (sauf --force). Aucune deduplication.

Les messages sont encodes par lots **inter-conversations** (un seul appel
`encode`) pour maximiser l'usage CPU et eviter le surcout par conversation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlite_vec import serialize_float32

from .embeddings import Embedder, text_fingerprint
from .store import VectorStore

DEFAULT_CHUNK = 64


def iter_conversation_files(
    exports_dir: Path | str, platforms: Optional[List[str]] = None
) -> Iterable[Tuple[str, Path]]:
    root = Path(exports_dir)
    for list_path in sorted(root.glob("*/conversation_list.json")):
        platform = list_path.parent.name
        if platforms and platform not in platforms:
            continue
        for json_file in sorted(list_path.parent.glob("*.json")):
            if json_file.name == "conversation_list.json":
                continue
            yield platform, json_file


def _flush(
    chunk: List[Dict[str, Any]],
    store: VectorStore,
    embedder: Embedder,
    stats: Dict[str, int],
    log: Callable[[str], None],
) -> None:
    texts = [text for conv in chunk for text in conv["texts"]]
    fingerprints = [text_fingerprint(embedder.model_name, text) for text in texts]

    # reutilisation : tout vecteur deja present en SQLite n'est pas recalcule
    cached = store.get_cached_vectors(fingerprints)
    missing = [
        (i, text)
        for i, (fp, text) in enumerate(zip(fingerprints, texts))
        if fp not in cached
    ]
    stats["vectors_reused"] += len(texts) - len(missing)
    if missing:
        vectors = embedder.encode([text for _, text in missing])
        for (index, _), vector in zip(missing, vectors):
            cached[fingerprints[index]] = serialize_float32(vector)
        stats["vectors_computed"] += len(missing)

    offset = 0
    for conv in chunk:
        count = len(conv["texts"])
        conv_fingerprints = fingerprints[offset:offset + count]
        conv_vectors = [cached[fp] for fp in conv_fingerprints]
        offset += count
        written = store.replace_conversation(
            conv["platform"],
            conv["conversation_id"],
            conv["title"],
            conv["exported_at"],
            conv["messages"],
            conv_vectors,
            conversation_file=str(conv["file"]),
            fingerprints=conv_fingerprints,
            url=conv.get("url"),
        )
        stats["conversations_indexed"] += 1
        stats["messages"] += written
        log(f"[{conv['platform']}] +{written} messages  ({conv['title'] or conv['conversation_id']})")


def index_exports(
    store: VectorStore,
    embedder: Embedder,
    exports_dir: Path | str,
    platforms: Optional[List[str]] = None,
    force: bool = False,
    limit: Optional[int] = None,
    chunk_size: int = DEFAULT_CHUNK,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    stats = {
        "conversations_indexed": 0,
        "conversations_skipped": 0,
        "messages": 0,
        "vectors_reused": 0,
        "vectors_computed": 0,
    }
    # renseigne les fingerprints des bases creees avant le cache de vecteurs
    store.ensure_fingerprints(embedder.model_name)
    chunk: List[Dict[str, Any]] = []

    for platform, json_file in iter_conversation_files(exports_dir, platforms):
        if limit is not None and stats["conversations_indexed"] >= limit:
            break
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"  ECHEC lecture {json_file.name}: {exc}")
            continue
        conversation_id = str(data.get("conversation_id") or "")
        if not conversation_id:
            continue
        exported_at = data.get("exported_at")
        if (
            not force
            and exported_at
            and store.indexed_exported_at(platform, conversation_id) == exported_at
        ):
            stats["conversations_skipped"] += 1
            continue

        messages = [
            m for m in (data.get("messages") or [])
            if (m.get("texte") or "").strip()
        ]
        chunk.append(
            {
                "platform": platform,
                "conversation_id": conversation_id,
                "title": data.get("title") or "",
                "url": data.get("url"),
                "exported_at": exported_at,
                "messages": messages,
                "texts": [m["texte"] for m in messages],
                "file": json_file,
            }
        )
        if len(chunk) >= max(1, chunk_size):
            _flush(chunk, store, embedder, stats, log)
            chunk = []

    if chunk:
        _flush(chunk, store, embedder, stats, log)
    return stats
