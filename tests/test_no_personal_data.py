"""Garde-fou : aucune trace personnelle dans les fichiers versionnes.

Reutilise les regles de `scripts/sanitize.py` sur l'arbre git suivi : noms,
emails, chemins locaux, JWT, jetons SAS, UUID reels (fichiers de fixtures).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sanitize import (  # noqa: E402
    _targets,
    check_text,
    scan_text,
)

EXCLUDED = {"scripts/sanitize.py", "tests/test_no_personal_data.py"}


def _tracked_files() -> List[Path]:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return [ROOT / name for name in result.stdout.decode("utf-8").split("\0") if name]


def test_aucune_trace_personnelle():
    problems: List[str] = []
    targets = {path.resolve() for path in _targets()}
    for path in _tracked_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXCLUDED or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.resolve() in targets:
            problems.extend(check_text(text, rel))
        else:
            # les UUID de demonstration hors fixtures sont toleres
            problems.extend(scan_text(text, rel, check_uuids=False))
    assert not problems, "traces personnelles detectees:\n" + "\n".join(problems[:20])
