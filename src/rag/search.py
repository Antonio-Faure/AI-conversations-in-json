"""Recherche combinee : similarite cosinus (embeddings) + mots-cles exacts.

Les deux recherches tournent sur le store SQLite/sqlite-vec, puis les scores
sont normalises dans [0,1] et combines : `score = alpha*semantique + (1-alpha)*mots_cles`.
Aucun resume, aucune deduplication : un resultat par message.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import numpy as np
from sqlite_vec import serialize_float32

from .store import VectorStore

TOKEN_RE = re.compile(r"[\w'’\-]+", re.UNICODE)


def tokenize(text: str, min_len: int = 2) -> List[str]:
    seen: List[str] = []
    for token in TOKEN_RE.findall((text or "").lower()):
        if len(token) >= min_len and token not in seen:
            seen.append(token)
    return seen


def keyword_score(texte: str, terms: List[str], phrase: str = "") -> float:
    """Fraction de termes presents, plus bonus si la phrase exacte apparait."""
    if not terms:
        return 0.0
    low = (texte or "").lower()
    matched = sum(1 for term in terms if term in low)
    score = matched / len(terms)
    if phrase and len(phrase) > 3 and phrase.lower() in low:
        score = min(1.0, score + 0.3)
    return score


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _filters(
    platforms: Optional[List[str]],
    model: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple:
    clauses: List[str] = []
    params: List[Any] = []
    if platforms:
        clauses.append("m.platform IN (" + ",".join("?" * len(platforms)) + ")")
        params.extend(platforms)
    if model:
        clauses.append("m.model = ?")
        params.append(model)
    if date_from:
        clauses.append("m.timestamp >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("m.timestamp <= ?")
        params.append(date_to)
    return clauses, params


def semantic_rows(
    store: VectorStore,
    query_vector,
    k: int,
    platforms=None,
    model=None,
    date_from=None,
    date_to=None,
) -> List[Dict[str, Any]]:
    clauses, params = _filters(platforms, model, date_from, date_to)
    where = " AND ".join(["v.embedding MATCH ?", "k = ?"] + clauses)
    sql = f"""
        SELECT m.rowid AS rowid, v.distance AS distance,
               m.message_id, m.conversation_id, m.platform, m.role, m.model,
               m.timestamp, m.texte, m.conversation_file
        FROM vec_messages v JOIN messages m ON m.rowid = v.rowid
        WHERE {where}
        ORDER BY v.distance
    """
    rows = store.db.execute(
        sql, [serialize_float32(query_vector), int(k)] + params
    ).fetchall()
    return [dict(row) for row in rows]


def keyword_rows(
    store: VectorStore,
    terms: List[str],
    platforms=None,
    model=None,
    date_from=None,
    date_to=None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    if not terms:
        return []
    clauses, params = _filters(platforms, model, date_from, date_to)
    like_clauses = []
    for term in terms:
        like_clauses.append("m.texte LIKE ? ESCAPE '\\'")
        params.append(_like(term))
    where = " AND ".join([f"({' OR '.join(like_clauses)})"] + clauses)
    rows = store.db.execute(
        f"""
        SELECT m.rowid AS rowid, m.message_id, m.conversation_id, m.platform,
               m.role, m.model, m.timestamp, m.texte, m.conversation_file
        FROM messages m
        WHERE {where}
        LIMIT ?
        """,
        params + [int(limit)],
    ).fetchall()
    return [dict(row) for row in rows]


def combined_search(
    store: VectorStore,
    query_vector,
    query_text: str,
    k: int = 20,
    alpha: float = 0.6,
    platforms: Optional[List[str]] = None,
    model: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    candidate_limit: int = 2000,
) -> List[Dict[str, Any]]:
    alpha = max(0.0, min(1.0, float(alpha)))
    terms = tokenize(query_text)
    query_vector = np.asarray(query_vector, dtype=np.float32)

    sem_k = min(max(k * 4, 200), 2000)
    sem = semantic_rows(
        store, query_vector, sem_k, platforms, model, date_from, date_to
    )
    semantic: Dict[int, float] = {r["rowid"]: 1.0 - float(r["distance"]) for r in sem}
    meta: Dict[int, Dict[str, Any]] = {r["rowid"]: r for r in sem}

    for row in keyword_rows(
        store, terms, platforms, model, date_from, date_to, limit=candidate_limit
    ):
        meta.setdefault(row["rowid"], row)

    # similarite manquante pour les candidats purement mots-cles
    missing = [rowid for rowid in meta if rowid not in semantic]
    if missing:
        for rowid, blob in store.vectors_by_rowid(missing).items():
            vec = np.frombuffer(blob, dtype=np.float32)
            semantic[rowid] = float(np.dot(query_vector, vec))

    phrase = query_text.strip()
    results: List[Dict[str, Any]] = []
    for rowid, info in meta.items():
        sem_score = max(0.0, min(1.0, semantic.get(rowid, 0.0)))
        kw_score = keyword_score(info.get("texte") or "", terms, phrase)
        score = alpha * sem_score + (1.0 - alpha) * kw_score
        if score <= 0:  # ni proche semantiquement, ni mot-cle
            continue
        results.append(
            {
                "rowid": rowid,
                "message_id": info.get("message_id") or "",
                "conversation_id": info.get("conversation_id"),
                "platform": info.get("platform"),
                "role": info.get("role"),
                "model": info.get("model"),
                "timestamp": info.get("timestamp"),
                "texte": info.get("texte") or "",
                "conversation_file": info.get("conversation_file"),
                "semantic_score": round(sem_score, 4),
                "keyword_score": round(kw_score, 4),
                "score": round(score, 4),
            }
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[: int(k)]
