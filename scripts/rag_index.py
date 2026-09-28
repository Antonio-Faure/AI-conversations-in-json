#!/usr/bin/env python3
"""Indexe les messages exportes dans le RAG (SQLite + sqlite-vec, BGE-M3).

Un vecteur par message. Incremental (une conversation non modifiee est ignoree)
sauf --force. Aucune deduplication.

    .venv/bin/python scripts/rag_index.py
    .venv/bin/python scripts/rag_index.py --platform grok --limit 5
    .venv/bin/python scripts/rag_index.py --force
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.rag.embeddings import Embedder  # noqa: E402
from src.rag.indexer import index_exports  # noqa: E402
from src.rag.store import VectorStore  # noqa: E402


def _resolve(path_value) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="base SQLite (defaut: config rag_db)")
    parser.add_argument("--exports", type=Path, help="racine des exports")
    parser.add_argument("--model", help="modele d'embeddings (defaut: config)")
    parser.add_argument("--platform", action="append", help="limiter a un service (repetable)")
    parser.add_argument("--force", action="store_true", help="reindexer meme si inchange")
    parser.add_argument("--limit", type=int, help="max de conversations (debug)")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    db_path = _resolve(args.db or config["rag_db"])
    exports_dir = _resolve(args.exports or config["output_dir"])
    model_name = args.model or config.get("rag_model")
    print(f"indexation: exports={exports_dir}")
    print(f"            db={db_path}  modele={model_name}")

    store = VectorStore(db_path)
    embedder = Embedder(model_name, batch_size=args.batch_size)
    started = time.time()
    stats = index_exports(
        store,
        embedder,
        exports_dir,
        platforms=args.platform,
        force=args.force,
        limit=args.limit,
    )
    elapsed = time.time() - started
    print(
        f"termine en {elapsed:.0f}s : {stats['conversations_indexed']} conversations "
        f"indexees, {stats['conversations_skipped']} ignorees, "
        f"{stats['messages']} messages"
    )
    print(
        f"vecteurs : {stats['vectors_reused']} reutilises (cache), "
        f"{stats['vectors_computed']} recalcules"
    )
    print("store:", store.stats())
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
