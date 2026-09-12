"""Stockage SQLite + sqlite-vec : un vecteur par message, recherche cosinus.

Tables :
  - messages        : metadonnees + texte de chaque message (rowid partage)
  - vec_messages    : vecteurs BGE-M3 (vec0, distance_metric=cosine)
  - conversations   : etat d'indexation (increment) par conversation
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlite_vec
from sqlite_vec import serialize_float32

from ..utils.file_utils import ensure_dir, now_iso_z
from .embeddings import DIM


class VectorStore:
    def __init__(self, db_path: Path | str, dim: int = DIM):
        self.db_path = Path(db_path)
        ensure_dir(self.db_path.parent)
        self.dim = int(dim)
        # check_same_thread=False : le serveur web sert depuis plusieurs threads
        # (acces serialise par le verrou du serveur).
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self._create_schema()

    def _create_schema(self) -> None:
        self.db.executescript(
            f"""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS messages (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT,
                conversation_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                role TEXT,
                model TEXT,
                timestamp TEXT,
                texte TEXT NOT NULL,
                has_code INTEGER DEFAULT 0,
                conversation_file TEXT,
                fingerprint TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv
                ON messages(platform, conversation_id);
            CREATE TABLE IF NOT EXISTS conversations (
                platform TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                title TEXT,
                url TEXT,
                exported_at TEXT,
                message_count INTEGER,
                indexed_at TEXT,
                PRIMARY KEY (platform, conversation_id)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_messages
                USING vec0(embedding float[{self.dim}] distance_metric=cosine);
            """
        )
        # migration : colonne fingerprint sur les bases existantes
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "fingerprint" not in columns:
            self.db.execute("ALTER TABLE messages ADD COLUMN fingerprint TEXT")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_fp ON messages(fingerprint)"
        )
        conv_columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(conversations)")
        }
        if "url" not in conv_columns:
            self.db.execute("ALTER TABLE conversations ADD COLUMN url TEXT")
        self.db.commit()

    # -- cache de vecteurs (fingerprint -> vecteur deja calcule) ---------------

    def ensure_fingerprints(self, model_name: str) -> int:
        """Renseigne les fingerprints manquants (bases creees avant le cache)."""
        from .embeddings import text_fingerprint

        rows = self.db.execute(
            "SELECT rowid, texte FROM messages WHERE fingerprint IS NULL"
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE messages SET fingerprint=? WHERE rowid=?",
                (text_fingerprint(model_name, row["texte"]), row["rowid"]),
            )
        if rows:
            self.db.commit()
        return len(rows)

    def get_cached_vectors(self, fingerprints: List[str]) -> Dict[str, bytes]:
        """Vecteurs deja stockes, indexes par fingerprint (aucun recalcul)."""
        unique = list(dict.fromkeys(fp for fp in fingerprints if fp))
        found: Dict[str, bytes] = {}
        chunk = 500
        for start in range(0, len(unique), chunk):
            batch = unique[start:start + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = self.db.execute(
                f"""
                SELECT m.fingerprint AS fp, v.embedding AS emb
                FROM messages m JOIN vec_messages v ON v.rowid = m.rowid
                WHERE m.fingerprint IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                found.setdefault(row["fp"], row["emb"])
        return found

    def close(self) -> None:
        self.db.close()

    # -- indexation ------------------------------------------------------------

    def indexed_exported_at(self, platform: str, conversation_id: str) -> Optional[str]:
        row = self.db.execute(
            "SELECT exported_at FROM conversations WHERE platform=? AND conversation_id=?",
            (platform, conversation_id),
        ).fetchone()
        return row["exported_at"] if row else None

    def _delete_conversation(self, platform: str, conversation_id: str) -> None:
        rowids = [
            r["rowid"]
            for r in self.db.execute(
                "SELECT rowid FROM messages WHERE platform=? AND conversation_id=?",
                (platform, conversation_id),
            ).fetchall()
        ]
        for rowid in rowids:
            self.db.execute("DELETE FROM vec_messages WHERE rowid=?", (rowid,))
        self.db.execute(
            "DELETE FROM messages WHERE platform=? AND conversation_id=?",
            (platform, conversation_id),
        )

    @staticmethod
    def _to_blob(vector) -> bytes:
        if isinstance(vector, (bytes, bytearray, memoryview)):
            return bytes(vector)
        return serialize_float32(vector)

    def replace_conversation(
        self,
        platform: str,
        conversation_id: str,
        title: str,
        exported_at: Optional[str],
        messages: List[Dict[str, Any]],
        vectors,
        conversation_file: Optional[str] = None,
        fingerprints: Optional[List[str]] = None,
        url: Optional[str] = None,
    ) -> int:
        """Remplace tous les messages d'une conversation (texte + vecteur).

        `vectors` peut contenir des vecteurs (numpy/list) ou des blobs deja
        serialises (reutilises depuis le cache).
        """
        self._delete_conversation(platform, conversation_id)
        count = 0
        for index, (message, vector) in enumerate(zip(messages, vectors)):
            fingerprint = fingerprints[index] if fingerprints else None
            cursor = self.db.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, platform, role, model,
                     timestamp, texte, has_code, conversation_file, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.get("message_id") or "",
                    conversation_id,
                    platform,
                    message.get("role") or "",
                    message.get("model") or "",
                    message.get("timestamp") or "",
                    message.get("texte") or "",
                    1 if message.get("code_blocks") else 0,
                    conversation_file,
                    fingerprint,
                ),
            )
            self.db.execute(
                "INSERT INTO vec_messages(rowid, embedding) VALUES (?, ?)",
                (cursor.lastrowid, self._to_blob(vector)),
            )
            count += 1
        self.db.execute(
            """
            INSERT INTO conversations
                (platform, conversation_id, title, url, exported_at, message_count, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, conversation_id) DO UPDATE SET
                title=excluded.title, url=COALESCE(excluded.url, conversations.url),
                exported_at=excluded.exported_at,
                message_count=excluded.message_count, indexed_at=excluded.indexed_at
            """,
            (platform, conversation_id, title, url, exported_at, count, now_iso_z()),
        )
        self.db.commit()
        return count

    # -- recherche -------------------------------------------------------------

    def search(
        self,
        query_vector,
        k: int = 10,
        platform: Optional[str] = None,
        role: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Top-k messages par similarite cosinus (distance = 1 - cosine)."""
        clauses = ["v.embedding MATCH ?", "k = ?"]
        params: List[Any] = [serialize_float32(query_vector), int(k)]
        if platform:
            clauses.append("m.platform = ?")
            params.append(platform)
        if role:
            clauses.append("m.role = ?")
            params.append(role)
        where = " AND ".join(clauses)
        rows = self.db.execute(
            f"""
            SELECT m.message_id, m.conversation_id, m.platform, m.role, m.model,
                   m.timestamp, m.texte, m.has_code, m.conversation_file,
                   v.distance AS distance
            FROM vec_messages v
            JOIN messages m ON m.rowid = v.rowid
            WHERE {where}
            ORDER BY v.distance
            LIMIT ?
            """,
            params + [int(k)],
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["score"] = 1.0 - float(item.pop("distance"))
            results.append(item)
        return results

    # -- acces par rowid (recherche combinee) -----------------------------------

    def vectors_by_rowid(self, rowids: List[int]) -> Dict[int, bytes]:
        if not rowids:
            return {}
        found: Dict[int, bytes] = {}
        chunk = 500
        for start in range(0, len(rowids), chunk):
            batch = rowids[start:start + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = self.db.execute(
                f"SELECT rowid, embedding FROM vec_messages WHERE rowid IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                found[row["rowid"]] = row["embedding"]
        return found

    def conversation_file(self, platform: str, conversation_id: str) -> Optional[str]:
        row = self.db.execute(
            """
            SELECT conversation_file FROM messages
            WHERE platform=? AND conversation_id=? AND conversation_file IS NOT NULL
            LIMIT 1
            """,
            (platform, conversation_id),
        ).fetchone()
        return row["conversation_file"] if row else None

    def distinct_models(self) -> List[str]:
        return [
            r["model"] for r in self.db.execute(
                "SELECT DISTINCT model FROM messages WHERE model != '' ORDER BY model"
            ).fetchall()
        ]

    def date_bounds(self) -> tuple:
        row = self.db.execute(
            "SELECT MIN(timestamp) AS lo, MAX(timestamp) AS hi FROM messages WHERE timestamp != ''"
        ).fetchone()
        return (row["lo"], row["hi"]) if row else (None, None)

    def conversation_meta(self, pairs) -> Dict[tuple, Dict[str, Any]]:
        """Titre + URL par (platform, conversation_id)."""
        found: Dict[tuple, Dict[str, Any]] = {}
        for platform, conversation_id in pairs:
            row = self.db.execute(
                "SELECT title, url FROM conversations WHERE platform=? AND conversation_id=?",
                (platform, conversation_id),
            ).fetchone()
            if row:
                found[(platform, conversation_id)] = {
                    "title": row["title"],
                    "url": row["url"],
                }
        return found

    def random_conversation(self) -> Optional[Dict[str, Any]]:
        row = self.db.execute(
            """
            SELECT platform, conversation_id, title, url
            FROM conversations
            WHERE message_count > 0
            ORDER BY RANDOM() LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    # -- stats -----------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        messages = self.db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        conversations = self.db.execute(
            "SELECT COUNT(*) AS n FROM conversations"
        ).fetchone()["n"]
        per_platform = {
            r["platform"]: r["n"]
            for r in self.db.execute(
                "SELECT platform, COUNT(*) AS n FROM messages GROUP BY platform"
            ).fetchall()
        }
        return {
            "messages": messages,
            "conversations": conversations,
            "per_platform": per_platform,
        }
