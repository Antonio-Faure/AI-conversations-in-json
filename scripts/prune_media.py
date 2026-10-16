"""Elagage des medias indexes (RAG).

Regles appliquees (voir le rapport de nettoyage) :
  - orphelin (sans conversation) dont le contenu existe aussi dans une copie
    liee a une conversation -> **relink** (la ligne media herite de la
    conversation du jumeau ; le fichier est conserve) ;
  - orphelin sans jumeau lie -> **suppression** (fichier + lignes media et
    vec_media) ;
  - copie referencee par un message (liee) -> conservee (l'affichage de la
    recherche dedoublonne par contenu).

Le dedoublonnage se fait par contenu (taille + md5 tete/queue), pas par nom
(le nom est un hash de l'URL : le meme contenu peut avoir plusieurs noms).

Par defaut : dry-run (aucune ecriture). `--apply` execute.

    .venv/bin/python scripts/prune_media.py            # plan
    .venv/bin/python scripts/prune_media.py --apply    # execute
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.rag.media import MediaStore  # noqa: E402

#: taille lue (tete et queue) pour la cle de contenu
HASH_BYTES = 256 * 1024


def content_key(path: Path) -> tuple[int, str] | None:
    """Cle de contenu d'un fichier (taille + md5 tete/queue) ; None si illisible."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    digest = hashlib.md5()
    try:
        with path.open("rb") as handle:
            digest.update(handle.read(HASH_BYTES))
            if size > 2 * HASH_BYTES:
                handle.seek(-HASH_BYTES, 2)
                digest.update(handle.read(HASH_BYTES))
    except OSError:
        return None
    return (size, digest.hexdigest())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "rag/messages-eg2.db")
    parser.add_argument("--exports", type=Path, default=ROOT / "exports")
    parser.add_argument(
        "--apply", action="store_true", help="execute (sinon dry-run)"
    )
    args = parser.parse_args()

    store = MediaStore(args.db, dim=768)
    rows = store.db.execute(
        "SELECT rowid, platform, conversation_id, message_id, kind, path FROM media"
    ).fetchall()
    groups: dict = defaultdict(list)
    for row in rows:
        key = content_key(args.exports / row["path"])
        if key is None:
            continue  # fichier illisible : jamais touche
        groups[key].append(dict(row))

    relink: list = []
    kill: list = []
    for items in groups.values():
        linked = [i for i in items if i["conversation_id"]]
        orphans = [i for i in items if not i["conversation_id"]]
        if not orphans:
            continue
        if linked:
            for orphan in orphans:
                # jumeau de meme plateforme d'abord (l'URL /media reste coherente)
                target = next(
                    (i for i in linked if i["platform"] == orphan["platform"]),
                    linked[0],
                )
                relink.append((orphan, target))
        else:
            kill.extend(orphans)

    print(f"medias indexes : {len(rows)} | groupes de contenu : {len(groups)}")
    print(f"a relier       : {len(relink)} orphelins (fichiers conserves)")
    print(f"a supprimer    : {len(kill)} orphelins (fichiers + lignes)")
    if relink:
        print("\nexemples de relink :")
        for orphan, target in relink[:4]:
            print(
                f"  {orphan['path'][:52]} -> {target['platform']}/"
                f"{str(target['conversation_id'])[:20]}"
            )
    if kill:
        print("\nexemples de suppression :")
        for orphan in kill[:6]:
            print(f"  {orphan['path'][:70]}")

    if not args.apply:
        print("\n(dry-run : rien n'a ete modifie ; relancer avec --apply)")
        return 0

    for orphan, target in relink:
        store.db.execute(
            "UPDATE media SET conversation_id = ?, message_id = ? WHERE rowid = ?",
            (target["conversation_id"], target["message_id"], orphan["rowid"]),
        )
    rowids = [i["rowid"] for i in kill]
    for chunk_start in range(0, len(rowids), 500):
        chunk = rowids[chunk_start : chunk_start + 500]
        marks = ",".join("?" * len(chunk))
        store.db.execute(f"DELETE FROM vec_media WHERE rowid IN ({marks})", chunk)
        store.db.execute(f"DELETE FROM media WHERE rowid IN ({marks})", chunk)
    store.db.commit()
    missing = 0
    for orphan in kill:
        path = args.exports / orphan["path"]
        try:
            path.unlink()
        except OSError:
            missing += 1
    print(
        f"\napplique : {len(relink)} relinks, {len(kill)} suppressions "
        f"({missing} fichiers deja absents)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
