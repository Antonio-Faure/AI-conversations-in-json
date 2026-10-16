"""Indexation des medias (images, audio, video) dans un store sqlite-vec dedie.

Un vecteur par fichier dans `vec_media` (meme dimension que le modele
d'embeddings), les metadonnees dans `media` (chemin, type, plateforme,
conversation/message d'origine, texte de reference). L'encodage vise l'espace
multimodal d'EmbeddingGemma 2 (texte/images/audio/video unifies) mais reste
utilisable avec n'importe quel encodeur fourni par l'appelant.

L'indexation est incrementale : un media dont le fingerprint (modele + chemin +
taille + mtime) est inchange n'est pas recalcule.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import sqlite_vec
from sqlite_vec import serialize_float32

from ..utils.file_utils import ensure_dir, now_iso_z

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}

#: references markdown ecrites par l'orchestrateur dans les messages
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(images/([^)]+)\)")
FILE_RE = re.compile(r"\[([^\]]*)\]\(files/([^)]+)\)")

#: fichiers de metadonnees a la racine d'une plateforme (pas des conversations)
RESERVED_JSON = {"conversation_list.json", "api_health.json"}

#: encodeur : liste de (kind, chemin) -> matrice de vecteurs (N x dim)
EncodeFn = Callable[[List[Tuple[str, Path]]], Any]


def media_kind(path: Path) -> Optional[str]:
    """Type de media d'un fichier (`image`/`audio`/`video`), sinon None."""
    ext = path.suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def iter_media_files(
    exports_dir: Path | str, platforms: Optional[List[str]] = None
) -> Iterator[Tuple[str, str, Path]]:
    """Parcourt les medias de l'archive : (plateforme, type, chemin)."""
    root = Path(exports_dir)
    for platform_dir in sorted(p for p in root.glob("*") if p.is_dir()):
        platform = platform_dir.name
        if platforms and platform not in platforms:
            continue
        images_dir = platform_dir / "images"
        if images_dir.is_dir():
            for path in sorted(images_dir.iterdir()):
                if path.is_file():
                    yield platform, "image", path
        files_dir = platform_dir / "files"
        if files_dir.is_dir():
            for path in sorted(files_dir.iterdir()):
                kind = media_kind(path)
                if path.is_file() and kind:
                    yield platform, kind, path


def build_reference_index(platform_dir: Path | str) -> Dict[Tuple[str, str], Dict[str, str]]:
    """Reference de chaque media : (dossier, fichier) -> conversation/message.

    Les messages contiennent `![alt](images/<fichier>)` et
    `[label](files/<fichier>)` : on recupere la conversation, le message et le
    texte de reference (alt ou label).
    """
    refs: Dict[Tuple[str, str], Dict[str, str]] = {}
    for json_file in sorted(Path(platform_dir).glob("*.json")):
        if json_file.name in RESERVED_JSON:
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        conversation_id = str(data.get("conversation_id") or "")
        if not conversation_id:
            continue
        for message in data.get("messages") or []:
            texte = str(message.get("texte") or "")
            if not texte:
                continue
            message_id = str(message.get("message_id") or "")
            for pattern, folder in ((IMAGE_RE, "images"), (FILE_RE, "files")):
                for label, name in pattern.findall(texte):
                    refs.setdefault(
                        (folder, name),
                        {
                            "conversation_id": conversation_id,
                            "message_id": message_id,
                            "texte": label.strip(),
                        },
                    )
    return refs


def media_fingerprint(model_name: str, rel_path: str, size: int, mtime: int) -> str:
    """Empreinte (modele, chemin, taille, mtime) : cle du cache de vecteurs."""
    payload = f"{model_name}\n{rel_path}\n{size}\n{mtime}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class MediaStore:
    """Tables `media` + `vec_media` (sqlite-vec) pour les medias.

    La dimension de `vec_media` suit la table existante si presente (comme
    `VectorStore`) ; sinon elle est creee avec `dim`.
    """

    def __init__(self, db_path: Path | str, dim: int):
        self.db_path = Path(db_path)
        ensure_dir(self.db_path.parent)
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        existing = self._existing_vec_dim()
        self.dim = existing if existing is not None else int(dim)
        self._create_schema()

    def _existing_vec_dim(self) -> Optional[int]:
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_media'"
        ).fetchone()
        if row is None or not row["sql"]:
            return None
        match = re.search(r"float\[(\d+)\]", row["sql"])
        return int(match.group(1)) if match else None

    def _create_schema(self) -> None:
        self.db.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS media (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                media_id TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL,
                conversation_id TEXT,
                message_id TEXT,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                texte TEXT,
                fingerprint TEXT,
                indexed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_media_platform ON media(platform);
            CREATE INDEX IF NOT EXISTS idx_media_fp ON media(fingerprint);
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_media
                USING vec0(embedding float[{self.dim}] distance_metric=cosine);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def indexed_fingerprints(self, media_ids: List[str]) -> Dict[str, str]:
        """Fingerprints des medias deja indexes (media_id -> fingerprint)."""
        found: Dict[str, str] = {}
        chunk = 500
        for start in range(0, len(media_ids), chunk):
            batch = media_ids[start:start + chunk]
            if not batch:
                continue
            placeholders = ",".join("?" * len(batch))
            rows = self.db.execute(
                f"SELECT media_id, fingerprint FROM media WHERE media_id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                found[row["media_id"]] = row["fingerprint"]
        return found

    def _delete(self, media_id: str) -> None:
        row = self.db.execute(
            "SELECT rowid FROM media WHERE media_id=?", (media_id,)
        ).fetchone()
        if row:
            self.db.execute("DELETE FROM vec_media WHERE rowid=?", (row["rowid"],))
            self.db.execute("DELETE FROM media WHERE rowid=?", (row["rowid"],))

    def replace(self, rows: List[Dict[str, Any]], vectors) -> int:
        """Insere/remplace des medias (rowid partage media <-> vec_media)."""
        count = 0
        for row, vector in zip(rows, vectors):
            self._delete(row["media_id"])
            cursor = self.db.execute(
                """
                INSERT INTO media
                    (media_id, platform, conversation_id, message_id, kind,
                     path, texte, fingerprint, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["media_id"],
                    row["platform"],
                    row.get("conversation_id") or "",
                    row.get("message_id") or "",
                    row["kind"],
                    row["path"],
                    row.get("texte") or "",
                    row.get("fingerprint") or "",
                    now_iso_z(),
                ),
            )
            self.db.execute(
                "INSERT INTO vec_media(rowid, embedding) VALUES (?, ?)",
                (cursor.lastrowid, serialize_float32(vector)),
            )
            count += 1
        self.db.commit()
        return count

    def search(self, query_vector, k: int = 10, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        """Top-k medias par similarite cosinus (option : filtrer par type)."""
        clauses = ["v.embedding MATCH ?", "k = ?"]
        params: List[Any] = [serialize_float32(query_vector), int(k)]
        if kind:
            clauses.append("v.rowid IN (SELECT rowid FROM media WHERE kind = ?)")
            params.append(kind)
        rows = self.db.execute(
            f"""
            SELECT m.media_id, m.platform, m.conversation_id, m.message_id, m.kind,
                   m.path, m.texte, v.distance AS distance
            FROM vec_media v
            JOIN media m ON m.rowid = v.rowid
            WHERE {" AND ".join(clauses)}
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

    def stats(self) -> Dict[str, Any]:
        counts = {
            r["kind"]: r["n"]
            for r in self.db.execute(
                "SELECT kind, COUNT(*) AS n FROM media GROUP BY kind"
            ).fetchall()
        }
        total = self.db.execute("SELECT COUNT(*) AS n FROM media").fetchone()["n"]
        return {"total": total, "per_kind": counts}


def index_media(
    store: MediaStore,
    encode_fn: EncodeFn,
    exports_dir: Path | str,
    model_name: str,
    platforms: Optional[List[str]] = None,
    image_batch: int = 4,
    log: Callable[[str], None] = print,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Indexe les medias de l'archive dans `store` (incremental).

    `encode_fn` recoit une liste de (kind, chemin) et renvoie les vecteurs.
    Les images sont encodees par lots (`image_batch`) ; audio et video un par un.
    """
    root = Path(exports_dir)
    refs_cache: Dict[str, Dict[Tuple[str, str], Dict[str, str]]] = {}
    stats: Dict[str, Any] = {
        "scanned": 0,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "per_kind": {"image": 0, "audio": 0, "video": 0},
    }

    pending: List[Tuple[Dict[str, Any], str, Path]] = []

    def flush() -> None:
        if not pending:
            return
        payloads = [(kind, path) for _, kind, path in pending]
        try:
            vectors = encode_fn(payloads)
        except Exception as exc:  # noqa: BLE001
            # un media corrompu ne doit pas arreter tout le build : reprise
            # media par media, les fautifs sont sautes (log).
            log(f"  lot en echec ({exc!r}) : reprise media par media")
            for row, kind, path in pending:
                try:
                    vectors = encode_fn([(kind, path)])
                except Exception as exc2:  # noqa: BLE001
                    log(f"  ECHEC {path} : {exc2!r}")
                    stats["failed"] += 1
                    continue
                if store.replace([row], vectors):
                    stats["per_kind"][kind] += 1
                    stats["indexed"] += 1
            pending.clear()
            return
        rows = [row for row, _, _ in pending]
        written = store.replace(rows, vectors)
        for row, _, _ in pending:
            stats["per_kind"][row["kind"]] += 1
        stats["indexed"] += written
        log(f"  +{written} medias ({pending[0][1]}, {pending[0][0]['platform']})")
        pending.clear()

    for platform, kind, path in iter_media_files(root, platforms):
        if limit is not None and stats["scanned"] >= limit:
            break
        stats["scanned"] += 1
        rel_path = str(path.relative_to(root))
        try:
            stat = path.stat()
        except OSError:
            stats["skipped"] += 1
            continue
        fingerprint = media_fingerprint(model_name, rel_path, stat.st_size, int(stat.st_mtime))
        cached = store.indexed_fingerprints([rel_path])
        if cached.get(rel_path) == fingerprint:
            stats["skipped"] += 1
            continue
        if platform not in refs_cache:
            refs_cache[platform] = build_reference_index(root / platform)
        folder = path.parent.name
        ref = refs_cache[platform].get((folder, path.name), {})
        row = {
            "media_id": rel_path,
            "platform": platform,
            "conversation_id": ref.get("conversation_id", ""),
            "message_id": ref.get("message_id", ""),
            "kind": kind,
            "path": rel_path,
            "texte": ref.get("texte", ""),
            "fingerprint": fingerprint,
        }
        if kind == "image":
            pending.append((row, kind, path))
            if len(pending) >= max(1, image_batch):
                flush()
        else:
            # les images en attente d'abord (l'ordre rows/vectors reste aligne)
            flush()
            pending.append((row, kind, path))
            flush()
    flush()
    return stats
