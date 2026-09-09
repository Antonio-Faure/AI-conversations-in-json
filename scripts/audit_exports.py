#!/usr/bin/env python3
"""Audit qualite des exports : liens, code, sources, artefacts DOM restants."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]+\]\((?:https?://|/)[^)]*\)|https?://[^\s)>]+")
FENCE_RE = re.compile(r"^```", re.MULTILINE)
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)|data:image/")
HTML_RE = re.compile(r"</?(?:div|span|p|a|button|svg|li|ul|table)[\s>/]")


def audit_dir(root: Path) -> int:
    issues: Counter = Counter()
    stats: Counter = Counter()
    files = sorted(root.glob("*/*/*.json"))
    if not files:
        print("aucun export")
        return 1
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        svc = d["service"]
        stats[f"{svc}:conv"] += 1
        stats[f"{svc}:msgs"] += len(d["messages"])
        if d["started_at"] is None:
            issues[f"{svc}:sans_timestamp"] += 1
        if d["model"] is None:
            issues[f"{svc}:sans_model"] += 1
        for m in d["messages"]:
            c = m["content"]
            stats[f"{svc}:liens"] += len(LINK_RE.findall(c))
            stats[f"{svc}:fences"] += len(FENCE_RE.findall(c))
            stats[f"{svc}:images"] += len(IMG_RE.findall(c))
            if HTML_RE.search(c):
                issues[f"{svc}:html_residuel"] += 1
                print(f"  HTML residuel {f.name} [{m['role']}]: "
                      f"{HTML_RE.search(c).group(0)!r} dans {c[:80]!r}")
            if len(c) < 4 and m["role"] == "assistant":
                issues[f"{svc}:msg_vide"] += 1
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
