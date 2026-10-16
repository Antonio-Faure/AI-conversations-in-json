#!/usr/bin/env python3
"""Recherche par similarite cosinus dans le RAG (embeddings + sqlite-vec).

    .venv/bin/python scripts/rag_search.py "comment configurer un serveur minecraft"
    .venv/bin/python scripts/rag_search.py "kubernetes" -k 20 --platform gemini
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.rag.embeddings import Embedder, model_dim  # noqa: E402
from src.rag.store import VectorStore  # noqa: E402
from src.utils.file_utils import resolve_cli_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="texte de la requete")
    parser.add_argument("-k", type=int, default=10, help="nombre de resultats")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml",
                        help="fichier de configuration (defaut: config.yaml)")
    parser.add_argument("--db", type=Path, help="base SQLite (defaut: config rag_db)")
    parser.add_argument("--model", help="modele d'embeddings (defaut: config)")
    parser.add_argument("--platform", help="filtrer sur un service")
    parser.add_argument("--role", choices=["user", "assistant", "system", "tool"])
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = resolve_cli_path(args.db or config["rag_db"], ROOT)
    model_name = args.model or config.get("rag_model")
    dim = model_dim(model_name, config.get("rag_dim"))

    store = VectorStore(db_path, dim=dim)
    embedder = Embedder(model_name, dim=dim)
    vector = embedder.encode_one(args.query)
    results = store.search(
        vector, k=args.k, platform=args.platform, role=args.role
    )
    print(f"requete: {args.query!r} | {len(results)} resultat(s)\n")
    for rank, item in enumerate(results, 1):
        snippet = " ".join((item["texte"] or "").split())[:300]
        print(
            f"{rank:2}. cos={item['score']:.3f} [{item['platform']}/{item['role']}] "
            f"{item['timestamp'] or '-'} conv={item['conversation_id']}"
        )
        print(f"    {snippet}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
