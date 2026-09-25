"""Metriques et controle de regression d'une conversation d'etalonnage.

L'etalonnage sert de canari : si le site change son DOM, le parseur perd des
capacites (images, tableaux, listes, langues de code, LaTeX...) et l'alternance
des roles peut casser. On resume ces signaux pour comparer dans le temps.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Set

#: capacites dont on suit la couverture (libelle -> cle de metrique)
FEATURES = (
    "messages",
    "consecutive_roles",
    "images",
    "tables",
    "lists",
    "code_langs",
    "latex",
)


def conversation_metrics(payload: Dict[str, Any]) -> Dict[str, int]:
    """Compte les signaux de qualite d'une conversation exportee."""
    messages = payload.get("messages") or []
    texts = [str(m.get("texte") or "") for m in messages]
    consecutive = sum(
        1
        for i in range(1, len(messages))
        if messages[i].get("role") == messages[i - 1].get("role")
    )
    images = sum(text.count("![") for text in texts)
    tables = sum(1 for text in texts if "| ---" in text or "|---" in text)
    unordered = sum(1 for text in texts if "\n- " in text or text.startswith("- "))
    ordered = sum(1 for text in texts if "\n1. " in text or text.startswith("1. "))
    code_langs = sum(
        1
        for message in messages
        for block in (message.get("code_blocks") or [])
        if block.get("language")
    )
    latex = sum(text.count("$") for text in texts)
    return {
        "messages": len(messages),
        "consecutive_roles": consecutive,
        "images": images,
        "tables": tables,
        "lists": unordered + ordered,
        "code_langs": code_langs,
        "latex": latex,
    }


#: capacites detectables depuis un export (sous-ensemble du manifeste de calibration)
_DETECTORS = {
    "headings": r"(?m)^#{1,3} ",
    "emphasis": r"\*\*[^*\n]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*)|~~[^~\n]+~~",
    "inline_code": r"`[^`\n]+`",
    "link": r"\]\(https?://",
    "blockquote": r"(?m)^\s*> ",
    "hr": r"(?m)^\s*(?:---|\*\*\*|___)\s*$",
    "list_bullet": r"(?m)^\s*[-*+] ",
    "list_numbered": r"(?m)^\s*\d+\. ",
    "checklist": r"(?m)^\s*[-*+] \[[ xX]\]",
    "list_nested": r"(?m)^\s{2,}[-*+] ",
    "table": r"\|\s*-{2,}",
    "latex_inline": r"(?<!\$)\$[^$\n]+\$(?!\$)",
    "latex_display": r"\$\$",
    "latex_matrix": r"\\begin\{(?:matrix|pmatrix|bmatrix|vmatrix|cases|aligned)\}",
    "image_markdown": r"!\[[^\]]*\]\(",
    "emoji_unicode": r"[\U0001F300-\U0001FAFF\u2600-\u27BF]|[\u0600-\u06FF]|[\u4E00-\u9FFF]",
    "refusal_error": r"(?i)\b(je ne peux pas|impossible|désolé|refus|je ne suis pas en mesure)\b",
}


#: capacites verifiables automatiquement depuis un export (les autres, comme les
#: pieces jointes ou le raisonnement, ne sont pas observables dans le JSON seul).
DETECTABLE = frozenset(_DETECTORS) | {
    "code_block", "code_languages", "code_long", "long_message",
}


def detect_capabilities(payload: Dict[str, Any]) -> Set[str]:
    """Capacites observables dans un export scraped (heuristique par motifs)."""
    messages = payload.get("messages") or []
    texts = [str(m.get("texte") or "") for m in messages]
    joined = "\n".join(texts)
    found = {cid for cid, pattern in _DETECTORS.items() if re.search(pattern, joined)}
    blocks = [b for m in messages for b in (m.get("code_blocks") or [])]
    if blocks:
        found.add("code_block")
    if any(b.get("language") for b in blocks):
        found.add("code_languages")
    if any(str(b.get("code") or "").count("\n") >= 8 for b in blocks):
        found.add("code_long")
    if any(len(text) >= 1200 for text in texts):
        found.add("long_message")
    return found


def normalize_for_match(text: str, limit: int = 30) -> str:
    """Cle de rapprochement d'un message (minuscule, sans accents, tronque)."""
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(ascii_only.lower().split())[:limit]


def queue_coverage(
    queue_texts: List[str], export_user_texts: List[str]
) -> Dict[str, List[int]]:
    """Indices (1-based) de la file presents dans l'export, et manquants."""
    keys = [normalize_for_match(t) for t in queue_texts]
    present = set()
    for text in export_user_texts:
        seen = normalize_for_match(text)
        for index, key in enumerate(keys, start=1):
            if key and (key in seen or seen in key):
                present.add(index)
                break
    return {
        "present": sorted(present),
        "missing": [i for i in range(1, len(queue_texts) + 1) if i not in present],
    }


def regressions(
    current: Dict[str, int], baseline: Dict[str, int], floor: float = 0.6
) -> List[str]:
    """Capacites perdues : passees de >=1 (baseline) a < floor * baseline."""
    lost: List[str] = []
    for feature in FEATURES:
        was = int(baseline.get(feature) or 0)
        now = int(current.get(feature) or 0)
        if was <= 0:
            continue
        if feature == "consecutive_roles":
            if now > was:
                lost.append(f"{feature}: {was} -> {now}")
        elif now < was * floor:
            lost.append(f"{feature}: {was} -> {now}")
    return lost
