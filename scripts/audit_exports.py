#!/usr/bin/env python3
"""Audit qualite des exports : liens, code, images, artefacts DOM restants.

Parcourt exports/<platform>/<conversation>.json (hors conversation_list.json).

Usage:
    .venv/bin/python scripts/audit_exports.py [exports]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]+\]\((?:https?://|/)[^)]*\)|https?://[^\s)>]+")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|data:image/")
HTML_RE = re.compile(
    r"</?(?:div|span|p|a|button|svg|li|ul|table)[\s>/]"
    r"|</?xai[A-Za-z]*|grok:render|</?argument\b"
)
FENCE_RE = re.compile(r"^```", re.MULTILINE)


def audit_dir(root: Path) -> int:
    issues: Counter = Counter()
    stats: Counter = Counter()
    files = [
        f
        for f in sorted(root.glob("*/*.json"))
        if f.name != "conversation_list.json"
    ]
    if not files:
        print("aucun export")
        return 1
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        platform = data.get("platform", f.parent.name)
        stats[f"{platform}:conv"] += 1
        messages = data.get("messages") or []
        stats[f"{platform}:msgs"] += len(messages)
        if data.get("started_at") is None:
            issues[f"{platform}:sans_timestamp"] += 1
        if data.get("model") is None:
            issues[f"{platform}:sans_model"] += 1
        for m in messages:
            text = m.get("texte", "")
            stats[f"{platform}:liens"] += len(LINK_RE.findall(text))
            stats[f"{platform}:fences"] += len(FENCE_RE.findall(text))
            stats[f"{platform}:images"] += len(IMG_RE.findall(text))
            stats[f"{platform}:code_blocks"] += len(m.get("code_blocks") or [])
            # html residuel : hors fences (le code cite contient du HTML legitime)
            no_fence = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
            if HTML_RE.search(no_fence):
                issues[f"{platform}:html_residuel"] += 1
                print(
                    f"  HTML residuel {f.name} [{m.get('role')}]: "
                    f"{HTML_RE.search(no_fence).group(0)!r} dans {text[:80]!r}"
                )
            if len(text) < 4 and m.get("role") == "assistant":
                issues[f"{platform}:msg_vide"] += 1
    print("\n== Stats ==")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    print("\n== Problemes ==")
    for k, v in sorted(issues.items()):
        print(f"  {k}: {v}")
    if not issues:
        print("  (aucun)")
    return 0


if __name__ == "__main__":
    sys.exit(audit_dir(Path(sys.argv[1] if len(sys.argv) > 1 else "exports")))
