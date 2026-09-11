"""Embeddings locaux BGE-M3 (multilingue FR/EN, offline).

Le modele n'est charge qu'a la premiere utilisation (lazy) : les scripts qui
n'ont pas besoin d'embeddings n'importent pas torch.
"""

from __future__ import annotations

import hashlib
import os
from typing import List, Optional

import numpy as np

DEFAULT_MODEL = "BAAI/bge-m3"
DIM = 1024


def text_fingerprint(model_name: str, texte: str) -> str:
    """Empreinte du couple (modele, texte) : cle du cache de vecteurs."""
    payload = f"{model_name}\n{texte or ''}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _configure_threads() -> None:
    """Limite le nombre de threads torch (ne pas saturer la machine)."""
    try:
        import torch

        env = os.environ.get("AICV_TORCH_THREADS")
        threads = int(env) if env else max(1, (os.cpu_count() or 2) - 1)
        torch.set_num_threads(threads)
    except Exception:  # noqa: BLE001
        pass


class Embedder:
    """Encode des textes en vecteurs normalises (cosine = produit scalaire)."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        batch_size: int = 16,
        device: Optional[str] = None,
    ):
        self.model_name = model_name
        self.batch_size = int(batch_size)
        self.device = device
        self._model = None

    def _load(self):
        if self._model is None:
            _configure_threads()
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    @property
    def dim(self) -> int:
        return DIM

    def encode(self, texts: List[str]) -> np.ndarray:
        """Vecteurs float32 normalises (shape N x DIM)."""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        model = self._load()
        vectors = model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.ascontiguousarray(vectors, dtype=np.float32)

    def encode_one(self, text: str) -> np.ndarray:
        return self.encode([text])[0]
