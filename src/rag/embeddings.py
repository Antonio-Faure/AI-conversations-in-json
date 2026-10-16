"""Embeddings locaux (EmbeddingGemma 2 par defaut), offline.

Le modele n'est charge qu'a la premiere utilisation (lazy) : les scripts qui
n'ont pas besoin d'embeddings n'importent pas torch.

Profils de modeles reconnus :
  - google/embeddinggemma-2 : 768 dims, prompts de tache obligatoires
    (requete : "task: search result | query: " ; document : "title: none | text: ").
  - les autres modeles : encodes brut, dimension deduite du nom (1024 par defaut).

La dimension est configurable (`rag_dim` dans la config) ; 0 = deduite du nom.
"""

from __future__ import annotations

import hashlib
import os
from typing import List, Optional

import numpy as np

DEFAULT_MODEL = "google/embeddinggemma-2"
DIM = 1024  # dimension hors EmbeddingGemma (fallback)
GEMMA_MODEL = "google/embeddinggemma-2"
GEMMA_DIM = 768
# noms des prompts declares dans config_sentence_transformers.json du modele :
#   SearchQuery -> "task: search result | query: "
#   Document    -> "title: none | text: "
GEMMA_QUERY_PROMPT = "SearchQuery"
GEMMA_DOCUMENT_PROMPT = "Document"
#: contexte d'embedding d'EmbeddingGemma (le tokenizer annonce une limite
#: quasi infinie : sans plafond, un message tres long fait exploser la RAM)
GEMMA_MAX_TOKENS = 8192


def is_embeddinggemma(model_name: str) -> bool:
    """Vrai si le modele est de la famille EmbeddingGemma (prompts requis)."""
    return "embeddinggemma" in (model_name or "").lower()


def model_dim(model_name: str, dim: Optional[int] = None) -> int:
    """Dimension d'embedding : explicite > deduite du modele > 1024."""
    if dim:
        return int(dim)
    if is_embeddinggemma(model_name):
        return GEMMA_DIM
    return DIM


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
    """Encode des textes en vecteurs normalises (cosine = produit scalaire).

    `encode` traite des documents (indexation) et `encode_one` des requetes
    (recherche) : les modeles a prompts (EmbeddingGemma) recoivent le prefixe
    de tache adapte, les autres modeles sont encodes brut.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        dim: Optional[int] = None,
        batch_size: int = 16,
        device: Optional[str] = None,
    ):
        self.model_name = model_name
        self.dim = model_dim(model_name, dim)
        self.batch_size = int(batch_size)
        self.device = device
        self._model = None

    def _load(self):
        if self._model is None:
            _configure_threads()
            from sentence_transformers import SentenceTransformer

            kwargs = {}
            if is_embeddinggemma(self.model_name):
                # texte seul : les encodeurs vision/audio (470M params) sont
                # inutiles ici et coutent ~1,8 Go de RAM.
                kwargs["config_kwargs"] = {"vision_config": None, "audio_config": None}
            model = SentenceTransformer(self.model_name, device=self.device, **kwargs)
            if is_embeddinggemma(self.model_name):
                # troncature au contexte d'embedding (8192 tokens) : le
                # tokenizer d'EmbeddingGemma annonce ~1e30, ce qui laisse
                # passer des messages de 50k+ tokens (activations d'attention
                # hors de controle) ; les autres modeles sont deja plafonnes a 8192.
                model.max_seq_length = min(model.max_seq_length, GEMMA_MAX_TOKENS)
            self._model = model
        return self._model

    def _prompt(self, kind: str) -> Optional[str]:
        """Nom du prompt de tache (EmbeddingGemma) ou None (sans prompts)."""
        if not is_embeddinggemma(self.model_name):
            return None
        return GEMMA_DOCUMENT_PROMPT if kind == "document" else GEMMA_QUERY_PROMPT

    def _as_text_inputs(self, texts: List[str]):
        """Entrees texte a passer au modele.

        EmbeddingGemma : le processor multimodal classe tout str qui ressemble
        a une URL (http(s) sans espace, domaine YouTube, extension image/audio/
        video) comme un media a telecharger (crash yt_dlp ou requete reseau).
        On force donc le format texte explicite `{"text": ...}`, strictement
        equivalent au str (meme texte encode, verifie empiriquement) ; les
        autres modeles n'ont pas ce processor et gardent des str.
        """
        if not is_embeddinggemma(self.model_name):
            return list(texts)
        return [{"text": text} for text in texts]

    def encode(self, texts: List[str], kind: str = "document") -> np.ndarray:
        """Vecteurs float32 normalises (shape N x dim).

        `kind` : "document" (indexation, defaut) ou "query" (recherche).

        Les textes sont encodes tries par longueur (lots homogenes : evite le
        padding massif d'un texte court par un texte long), puis remis dans
        l'ordre d'entree. Chaque texte est encode independamment : le tri ne
        change pas les vecteurs produits.
        """
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        model = self._load()
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        sorted_texts = [texts[i] for i in order]
        vectors = model.encode(
            self._as_text_inputs(sorted_texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
            prompt_name=self._prompt(kind),
        )
        vectors = np.asarray(vectors, dtype=np.float32)
        if order != list(range(len(texts))):
            restored = np.empty_like(vectors)
            for position, original in enumerate(order):
                restored[original] = vectors[position]
            vectors = restored
        return np.ascontiguousarray(vectors, dtype=np.float32)

    def encode_one(self, text: str, kind: str = "query") -> np.ndarray:
        return self.encode([text], kind=kind)[0]
