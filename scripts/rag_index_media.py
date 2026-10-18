#!/usr/bin/env python3
"""Indexe les medias (images, audio, video) dans un index sqlite-vec dedie.

Encodeur : EmbeddingGemma 2 complet (espace multimodal unifie texte/images/
audio/video). Les images utilisent un budget vision reduit (--image-budget,
defaut 70 soft tokens) pour tenir les temps de calcul CPU ; les audio/video
sont encodes tels quels (pas de prefixe de tache sur les medias).

    .venv/bin/python scripts/rag_index_media.py
    .venv/bin/python scripts/rag_index_media.py --platform gemini
    .venv/bin/python scripts/rag_index_media.py --limit 5
    .venv/bin/python scripts/rag_index_media.py --search "un paysage de montagne"

La table `media` + `vec_media` vit dans la meme base que l'index texte
(rag_db de la config). L'indexation est incrementale (fingerprint modele +
chemin + taille + mtime) : relancer saute les medias deja encodess.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.rag.embeddings import is_embeddinggemma, model_dim  # noqa: E402
from src.rag.media import MediaStore, index_media  # noqa: E402
from src.utils.file_utils import resolve_cli_path  # noqa: E402


def load_multimodal_model(model_name: str, device: str | None = None):
    """Charge SentenceTransformer complet (texte + vision + audio)."""
    import torch
    from sentence_transformers import SentenceTransformer

    env = os.environ.get("AICV_TORCH_THREADS")
    threads = int(env) if env else max(1, (os.cpu_count() or 2) - 1)
    torch.set_num_threads(threads)
    return SentenceTransformer(
        model_name,
        device=device,
        model_kwargs={"torch_dtype": torch.float32},
    )


def _local_media_path(path: Path, root: Path) -> str:
    """Chemin local sous `exports/` uniquement : jamais une URL distante.

    Le processor multimodal telecharge tout ce qui ressemble a une URL ; ici on
    ne passe que des fichiers locaux de l'archive (defense en profondeur).
    """
    resolved = Path(path).resolve()
    root_resolved = root.resolve()
    if not resolved.is_file() or root_resolved not in resolved.parents:
        raise ValueError(f"media local attendu sous {root_resolved} : {path}")
    return str(resolved)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml",
                        help="fichier de configuration (defaut: config.yaml)")
    parser.add_argument("--db", type=Path, help="base SQLite (defaut: config rag_db)")
    parser.add_argument("--exports", type=Path, help="racine des exports")
    parser.add_argument("--model", help="modele d'embeddings (defaut: config)")
    parser.add_argument("--platform", action="append", help="limiter a un service (repetable)")
    parser.add_argument("--image-budget", type=int, default=70,
                        help="budget vision par image : 70/140/280/560/1120 (defaut: 70)")
    parser.add_argument("--image-batch", type=int, default=4,
                        help="images encodees par lot (defaut: 4)")
    parser.add_argument("--limit", type=int, help="max de medias scannes (debug)")
    parser.add_argument("--search", help="requete texte -> top-k medias (lecture seule)")
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--kind", choices=["image", "audio", "video"],
                        help="filtrer le type (avec --search)")
    args = parser.parse_args()

    config = load_config(args.config)
    db_path = resolve_cli_path(args.db or config["rag_db"], ROOT)
    exports_dir = resolve_cli_path(args.exports or config["output_dir"], ROOT)
    model_name = args.model or config.get("rag_model")
    dim = model_dim(model_name, config.get("rag_dim"))

    store = MediaStore(db_path, dim=dim)

    # -- requete texte -> medias (aucune ecriture) -----------------------------
    if args.search:
        from src.rag.embeddings import Embedder

        embedder = Embedder(model_name, dim=dim)
        vector = embedder.encode_one(args.search)
        results = store.search(vector, k=args.k, kind=args.kind)
        print(f"requete: {args.search!r} | {len(results)} media(s)\n")
        for rank, item in enumerate(results, 1):
            print(
                f"{rank:2}. cos={item['score']:.3f} [{item['kind']:5}] "
                f"{item['path']}  conv={item['conversation_id'] or '-'}"
            )
            if item["texte"]:
                print(f"    ref: {item['texte'][:120]}")
        store.close()
        return 0

    print(f"medias: exports={exports_dir}")
    print(f"        db={db_path}  modele={model_name}  dim={store.dim}")
    if not is_embeddinggemma(model_name):
        print("attention: l'espace multimodal attendu est celui d'EmbeddingGemma 2")

    started = time.time()
    # chargement paresseux du modele multimodal : s'il n'y a aucun media a
    # encoder, il n'est jamais charge (cron : ~1 s au lieu de ~40 s de CPU).
    holder: Dict[str, Any] = {}

    def encode_fn(payloads: List[Tuple[str, Path]]) -> np.ndarray:
        """Encode une liste ordonnee de (kind, chemin) -> vecteurs alignes."""
        if "model" not in holder:
            print("chargement du modele multimodal (premiers medias a encoder)…")
            holder["model"] = load_multimodal_model(model_name)
        model = holder["model"]
        chunks: List[np.ndarray] = []
        index = 0
        while index < len(payloads):
            kind, path = payloads[index]
            if kind == "image":
                end = index
                while end < len(payloads) and payloads[end][0] == "image":
                    end += 1
                images = [
                    {"image": _local_media_path(p, exports_dir)}
                    for _, p in payloads[index:end]
                ]
                vectors = model.encode(
                    images,
                    batch_size=len(images),
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    processing_kwargs={"image": {"max_soft_tokens": args.image_budget}},
                )
                chunks.append(np.asarray(vectors, dtype="float32"))
                index = end
            else:
                vector = model.encode(
                    {kind: _local_media_path(path, exports_dir)},
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                chunks.append(np.asarray(vector, dtype="float32").reshape(1, -1))
                index += 1
        return np.concatenate(chunks, axis=0)

    stats: Dict[str, Any] = index_media(
        store,
        encode_fn,
        exports_dir,
        model_name,
        platforms=args.platform,
        image_batch=args.image_batch,
        limit=args.limit,
    )
    elapsed = time.time() - started
    print(
        f"termine en {elapsed:.0f}s : {stats['indexed']} medias indexes "
        f"({stats['per_kind']}), {stats['skipped']} ignores, {stats['failed']} en echec"
    )
    if not holder:
        print("aucun media a indexer : modele non charge")
    print("store:", store.stats())
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
