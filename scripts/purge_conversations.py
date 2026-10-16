"""Purge locale de conversations (supprimees sur les plateformes).

Pour chaque conversation listee :
  - export JSON standardise (+ `.json.bak`) et payload brut `raw/<slug>.json` ;
  - dossier de screenshots eventuel ;
  - entree dans `exports/<plateforme>/conversation_list.json` ;
  - lignes RAG (messages + vecteurs + conversations) dans l'index
    EmbeddingGemma (`rag/messages-eg2.db`) ;
  - medias rattaches (fichier + lignes media/vec_media) **sauf** si le fichier
    est encore reference par une autre conversation (securite).

    .venv/bin/python scripts/purge_conversations.py \
        --from-file /tmp/opencode/calibration_conversations.json --cats A,B,C
    # + --apply pour executer (dry-run par defaut)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils.file_utils import load_conversation_list, save_conversation_list  # noqa: E402

#: base RAG a purger (index EmbeddingGemma)
RAG_DBS = ("rag/messages-eg2.db",)
#: references de fichiers dans les JSON (`images/<hash>.png`, `files/<hash>.mp3`)
REF_RE = re.compile(r"(?:images|files)/([A-Za-z0-9._-]+?)(?:\?|[\s)\"'])")


def open_db(path: Path) -> sqlite3.Connection:
    """Ouvre une base RAG (extension sqlite-vec chargee)."""
    import sqlite_vec

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    return row is not None


def referenced_filenames(exports: Path, skip: set) -> set:
    """Noms de fichiers encore references par les JSON (hors conversations purgees)."""
    names: set = set()
    for platform_dir in exports.iterdir():
        if not platform_dir.is_dir():
            continue
        for path in platform_dir.glob("*.json"):
            if path.name == "conversation_list.json" or path.suffix == ".bak":
                continue
            if str(path) in skip:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names.update(REF_RE.findall(text))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-file", type=Path, required=True)
    parser.add_argument(
        "--cats",
        default="",
        help="categories a purger (ex. A,B,C ; vide = toutes)",
    )
    parser.add_argument("--apply", action="store_true", help="execute (sinon dry-run)")
    args = parser.parse_args()

    allowed = {c.strip() for c in args.cats.split(",") if c.strip()}
    entries = json.loads(args.from_file.read_text(encoding="utf-8"))
    selected = [
        e for e in entries if not allowed or set(e["cats"]) <= allowed
    ]
    print(f"{len(selected)} conversations a purger (sur {len(entries)} listees)")
    for e in selected:
        print(
            f"  [{'/'.join(e['cats'])}] {e['platform']:10} "
            f"{e['conversation_id'][:36]:38} {e['msgs']:3} msg  {e['title'][:44]}"
        )
    if not args.apply:
        print("\n(dry-run : rien n'a ete modifie ; relancer avec --apply)")
        return 0

    exports = ROOT / "exports"
    skip = {str(ROOT / e["file"]) for e in selected if e.get("file")}
    referenced = referenced_filenames(exports, skip)
    print(f"\n{len(referenced)} noms de fichiers encore references ailleurs")

    # conversation_list.json : retirer les entrees
    removed_list: dict = {}
    by_platform: dict = {}
    for e in selected:
        by_platform.setdefault(e["platform"], []).append(e["conversation_id"])
    for platform, ids in by_platform.items():
        list_path = exports / platform / "conversation_list.json"
        if not list_path.exists():
            continue
        index = load_conversation_list(list_path)
        for cid in ids:
            if index.pop(cid, None) is not None:
                removed_list[platform] = removed_list.get(platform, 0) + 1
        save_conversation_list(list_path, platform, index)

    # fichiers d'exports
    deleted_files = 0
    for e in selected:
        json_path = ROOT / e["file"]
        targets = [
            json_path,
            json_path.with_suffix(".json.bak"),
            exports / e["platform"] / "raw" / json_path.name,
        ]
        for target in targets:
            if target.exists():
                target.unlink()
                deleted_files += 1
        shots = exports / e["platform"] / "screenshots" / json_path.stem
        if shots.is_dir():
            shutil.rmtree(shots)
            deleted_files += 1

    # bases RAG (messages/vecteurs/conversations + medias)
    db_stats: dict = {}
    kept_media: list = []
    for rel in RAG_DBS:
        db_path = ROOT / rel
        if not db_path.exists():
            continue
        conn = open_db(db_path)
        stats = {"messages": 0, "conversations": 0, "media": 0}
        for e in selected:
            platform, cid = e["platform"], e["conversation_id"]
            rowids = [
                r["rowid"]
                for r in conn.execute(
                    "SELECT rowid FROM messages WHERE platform = ? AND conversation_id = ?",
                    (platform, cid),
                )
            ]
            if rowids:
                marks = ",".join("?" * len(rowids))
                if has_table(conn, "vec_messages"):
                    conn.execute(
                        f"DELETE FROM vec_messages WHERE rowid IN ({marks})", rowids
                    )
                conn.execute(f"DELETE FROM messages WHERE rowid IN ({marks})", rowids)
                stats["messages"] += len(rowids)
            cur = conn.execute(
                "DELETE FROM conversations WHERE platform = ? AND conversation_id = ?",
                (platform, cid),
            )
            stats["conversations"] += cur.rowcount or 0
            if has_table(conn, "media"):
                media_rows = conn.execute(
                    "SELECT rowid, path FROM media WHERE platform = ? AND conversation_id = ?",
                    (platform, cid),
                ).fetchall()
                for row in media_rows:
                    filename = Path(row["path"]).name
                    if filename in referenced:
                        # fichier encore utilise ailleurs : on garde le fichier,
                        # mais on detache la ligne de la conversation purgee
                        conn.execute(
                            "UPDATE media SET conversation_id = NULL, message_id = NULL"
                            " WHERE rowid = ?",
                            (row["rowid"],),
                        )
                        kept_media.append(row["path"])
                        continue
                    conn.execute(
                        "DELETE FROM vec_media WHERE rowid = ?", (row["rowid"],)
                    )
                    conn.execute("DELETE FROM media WHERE rowid = ?", (row["rowid"],))
                    try:
                        (exports / row["path"]).unlink()
                    except OSError:
                        pass
                    stats["media"] += 1
        conn.commit()
        conn.close()
        db_stats[rel] = stats

    print(f"\nfichiers d'exports supprimes : {deleted_files}")
    for platform, n in sorted(removed_list.items()):
        print(f"  conversation_list {platform}: -{n}")
    for rel, stats in db_stats.items():
        print(
            f"  {rel}: -{stats['messages']} messages, "
            f"-{stats['conversations']} conversations, -{stats['media']} medias"
        )
    if kept_media:
        print(f"  medias conserves (encore references ailleurs) : {len(kept_media)}")
        for path in kept_media[:5]:
            print(f"    {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
