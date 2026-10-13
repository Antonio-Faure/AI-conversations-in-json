#!/usr/bin/env python3
"""Regenere les exports a partir des artefacts bruts (sans re-crawler).

- plateformes API (ChatGPT, Claude, Gemini, Grok, Mistral, Perplexity) :
  re-mappe depuis le payload brut `raw/<stem>.json`
  (`exports/<platform>/raw/`) ; le payload prime sur un `.html` residuel d'un
  ancien scrape DOM (migration JSON-only) ;
- repli DOM (`services.<bot>.api: false` pour chatgpt/claude/mistral/perplexity,
  `dom: true` pour perplexity, defaut pour gemini sans `api: true`) : re-parse
  le `.html` voisin et fusionne les metadonnees absentes depuis l'ancien JSON
  (par message_id, sinon par index) ;
- une conversation sans artefact brut (HTML ou payload) est nettoyee
  directement (normalisation, balises xAI) ;
- toutes : normalisation de l'alternance user/assistant ;
- met a jour conversation_list.json (message_count, has_code, last_message_at).

Protections : les JSON non-conversation du dossier (`conversation_list.json`,
`api_health.json`, tout JSON sans `conversation_id`) sont ignores ; un artefact
brut tronque ne degrade pas un JSON existant plus complet (moins de messages
que l'archive -> conversation conservee et signalee, comme le statut `kept` de
l'orchestrateur).

    .venv/bin/python scripts/reparse_exports.py
    .venv/bin/python scripts/reparse_exports.py --platform gemini
    .venv/bin/python scripts/reparse_exports.py --no-backup
    .venv/bin/python scripts/reparse_exports.py --clean-backups

Une fois les JSON regeneres, relancer explicitement la reindexation RAG
(`scripts/rag_index.py --force`) : le cache de l'indexeur est base sur
`exported_at`, que le reparse conserve.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.parsers import PARSER_CLASSES  # noqa: E402
from src.schema import Conversation, Message, extract_code_blocks, normalize_messages  # noqa: E402
from src.utils.cleanup import clean_text  # noqa: E402
from src.utils.file_utils import (  # noqa: E402
    RESERVED_JSON_NAMES,
    merge_conversation_json,
    write_json_atomic,
)
from src.utils.raw_payload import load_raw_payload  # noqa: E402
from src.utils.urls import (  # noqa: E402
    canonical_url_from_html,
    conversation_url,
    normalize_conversation_url,
)


def _url_for(
    platform: str, conversation_id: str, html_path: Optional[Path] = None
) -> str:
    if html_path is not None and html_path.exists():
        canonical = canonical_url_from_html(
            html_path.read_text(encoding="utf-8", errors="replace")
        )
        if canonical and conversation_id in canonical:
            return normalize_conversation_url(canonical)
    return normalize_conversation_url(conversation_url(platform, conversation_id))

DOM_PLATFORMS = ("chatgpt", "claude", "gemini", "mistral", "perplexity")
#: plateformes API : payload brut dans raw/<stem>.json (prime sur un HTML
#: residuel pour les plateformes migrees : chatgpt, claude, gemini, grok,
#: mistral, perplexity)
API_PLATFORMS = ("chatgpt", "claude", "gemini", "grok", "mistral", "perplexity")


def _resolve(value) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _merge_old_metadata(conv: Conversation, old: Dict[str, Any]) -> Conversation:
    old_messages = old.get("messages") or []
    by_id = {
        m.get("message_id"): m for m in old_messages if m.get("message_id")
    }
    for index, message in enumerate(conv.messages):
        ref = by_id.get(message.message_id) if message.message_id else None
        if ref is None and index < len(old_messages):
            ref = old_messages[index]
        if not ref:
            continue
        if not message.timestamp and ref.get("timestamp"):
            message.timestamp = ref["timestamp"]
        if not message.model and ref.get("model"):
            message.model = ref["model"]
        if not message.message_id and ref.get("message_id"):
            message.message_id = ref["message_id"]
    conv.exported_at = old.get("exported_at") or conv.exported_at
    if not conv.model and old.get("model"):
        conv.model = old["model"]
    # les timestamps restaures doivent etre pris en compte pour started/last
    conv.derive_timestamps()
    return conv


def _reparse(platform: str, old: Dict[str, Any], html_path: Path) -> Conversation:
    parser = PARSER_CLASSES[platform]()
    conv = parser.parse(
        html_path.read_text(encoding="utf-8", errors="replace"),
        conversation_id=str(old.get("conversation_id") or ""),
        extra={"title_hint": old.get("title")},
    )
    conv = _merge_old_metadata(conv, old)
    if not conv.url:
        conv.url = _url_for(platform, conv.conversation_id, html_path)
    return conv


def _reparse_api(platform: str, old: Dict[str, Any], raw: Any) -> Conversation:
    """Re-mappe une conversation depuis le payload API brut (`raw/<stem>.json`).

    ChatGPT (`backend-api/conversations`), Claude (`chat_conversations/<id>`),
    Mistral (batch tRPC `message.all,chat.byId,canva.all`) et Perplexity
    (`/rest/thread/<id>`) passent le payload dans l'extra `api` ; Gemini
    (RPC `batchexecute hNvQHb`) y ajoute `title_hint` (le payload ne porte pas
    toujours de titre) ; Grok recoit `{conversation, responses}` (les deux
    reponses REST de l'API).
    """
    parser = PARSER_CLASSES[platform]()
    payload = raw if isinstance(raw, dict) else {}
    if platform == "grok":
        extra: Dict[str, Any] = {
            "conversation": payload.get("conversation") or {},
            "responses": payload.get("responses") or [],
        }
    elif platform == "gemini":
        extra = {"api": payload, "title_hint": old.get("title")}
    else:  # chatgpt, claude, mistral, perplexity : payload dans extra["api"]
        extra = {"api": payload}
    conv = parser.parse(
        "",
        conversation_id=str(old.get("conversation_id") or ""),
        extra=extra,
    )
    conv = _merge_old_metadata(conv, old)
    if not conv.url:
        conv.url = old.get("url") or _url_for(platform, conv.conversation_id)
    return conv


def _clean_json(platform: str, old: Dict[str, Any]) -> Conversation:
    messages = [Message.from_dict(m) for m in old.get("messages") or []]
    for message in messages:
        message.texte = clean_text(message.texte, platform)
        message.code_blocks = extract_code_blocks(message.texte)
    messages = normalize_messages(messages)
    return Conversation(
        platform=platform,
        conversation_id=str(old.get("conversation_id") or ""),
        title=old.get("title") or "",
        messages=messages,
        model=old.get("model"),
        started_at=old.get("started_at"),
        last_message_at=old.get("last_message_at"),
        exported_at=old.get("exported_at"),
        url=old.get("url"),
    )


def _clean_backups(exports: Path, platforms: List[str]) -> int:
    """Purge explicite des sauvegardes `.json.bak` (jamais automatique).

    Les `.bak` servent de repli de metadonnees lors d'un re-parse et ne sont
    pas nettoyes par les runs normaux ; cette option les supprime uniquement
    quand elle est demandee.
    """
    removed = 0
    for platform in platforms:
        for backup in sorted((exports / platform).glob("*.json.bak")):
            backup.unlink()
            removed += 1
    print(f"termine : {removed} sauvegarde(s) .json.bak supprimee(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path)
    parser.add_argument("--platform", action="append")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument(
        "--clean-backups", action="store_true",
        help="supprime les sauvegardes .json.bak des plateformes traitees, "
             "puis quitte (jamais automatique)",
    )
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    exports = _resolve(args.exports or config["output_dir"])
    platforms = args.platform or sorted(
        d.name for d in exports.iterdir()
        if d.is_dir() and (d / "conversation_list.json").exists()
    )
    if args.clean_backups:
        return _clean_backups(exports, platforms)

    totals = {"reparsed": 0, "remapped": 0, "cleaned": 0, "kept": 0,
              "ignored": 0, "failed": 0}
    for platform in platforms:
        directory = exports / platform
        list_path = directory / "conversation_list.json"
        index: Dict[str, Dict[str, Any]] = {}
        if list_path.exists():
            data = json.loads(list_path.read_text(encoding="utf-8"))
            index = {
                str(e.get("conversation_id")): e for e in data.get("conversations") or []
            }
        for json_path in sorted(directory.glob("*.json")):
            # fichiers non-conversation du dossier (inventaire, rapport de forme
            # API) : ne jamais les traiter comme des conversations
            if json_path.name in RESERVED_JSON_NAMES:
                continue
            try:
                old = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                totals["failed"] += 1
                continue
            if not isinstance(old, dict) or not old.get("conversation_id"):
                # JSON etranger au schema (sans id) : le laisser intact
                totals["ignored"] += 1
                continue
            # metadonnees d'origine (timestamps/model) : la sauvegarde .bak si
            # elle existe (elle precede un eventuel re-parse precedent)
            meta_path = json_path.with_suffix(".json.bak")
            old_meta = old
            if meta_path.exists():
                try:
                    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    old_meta = old
            # l'URL peut n'exister que dans le JSON courant (backfill posterieur)
            if not old_meta.get("url") and old.get("url"):
                old_meta["url"] = old["url"]
            # artefact brut : HTML (plateformes DOM) ou payload API (raw/)
            html_path = json_path.with_suffix(".html")
            raw = load_raw_payload(exports, platform, json_path.stem)
            try:
                # API d'abord : pour les plateformes migrees, un .html residuel
                # (ancien scrape DOM) ne doit pas gagner sur le payload brut
                if platform in API_PLATFORMS and raw is not None:
                    conv = _reparse_api(platform, old_meta, raw)
                    totals["remapped"] += 1
                elif platform in DOM_PLATFORMS and html_path.exists():
                    conv = _reparse(platform, old_meta, html_path)
                    totals["reparsed"] += 1
                else:
                    # pas d'artefact brut (conversation migree, export ancien) :
                    # nettoyage direct du JSON, sans planter
                    conv = _clean_json(platform, old_meta)
                    totals["cleaned"] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[{platform}] ECHEC {json_path.name}: {exc}")
                totals["failed"] += 1
                continue
            # protection d'integrite : un artefact brut tronque (scrape partiel)
            # produit moins de messages que le JSON existant -> ne pas degrader
            # l'archive (le prochain reparse complet la rafraichira)
            old_count = len(old.get("messages") or [])
            if old_count and len(conv.messages) < old_count:
                totals["kept"] += 1
                print(
                    f"[{platform}] conserve {json_path.name} "
                    f"(artefact brut tronque : {len(conv.messages)} < {old_count} messages)"
                )
                continue
            # fusion : les anciens messages sont un prefixe -> ajout des nouveaux
            # (meme regle que l'orchestrateur)
            merged, _status, _appended = merge_conversation_json(
                old, conv.to_dict(validate=False)
            )
            if not args.no_backup:
                backup = json_path.with_suffix(".json.bak")
                if not backup.exists():
                    shutil.copy2(json_path, backup)
            write_json_atomic(json_path, merged)
            entry = index.get(conv.conversation_id)
            if entry is not None:
                entry["title"] = conv.title or entry.get("title") or ""
                entry["message_count"] = len(conv.messages)
                entry["has_code"] = conv.has_code
                entry["last_message_at"] = conv.last_message_at
        if index and list_path.exists():
            data = json.loads(list_path.read_text(encoding="utf-8"))
            data["conversations"] = sorted(
                index.values(),
                key=lambda e: (e.get("last_message_at") or "", e.get("title") or ""),
                reverse=True,
            )
            data["count"] = len(data["conversations"])
            write_json_atomic(list_path, data)
        print(f"[{platform}] ok")

    print(
        f"termine : {totals['reparsed']} re-parses, {totals['remapped']} remappes "
        f"(payload API), {totals['cleaned']} nettoyes, {totals['kept']} conserves "
        f"(artefact tronque), {totals['ignored']} ignores (non-conversation), "
        f"{totals['failed']} echecs"
    )
    if totals["reparsed"] + totals["remapped"] + totals["cleaned"]:
        print(
            "rappel : relancer `.venv/bin/python scripts/rag_index.py --force` "
            "pour reindexer les JSON regeneres (exported_at conserve : sans "
            "--force, l'indexeur saute les conversations)"
        )
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
