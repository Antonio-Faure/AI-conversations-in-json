"""Metriques et controle de regression d'une conversation d'etalonnage.

L'etalonnage sert de canari : si le site change son DOM, le parseur perd des
capacites (images, tableaux, listes, langues de code, LaTeX...) et l'alternance
des roles peut casser. On resume ces signaux pour comparer dans le temps.
"""

from __future__ import annotations

from typing import Any, Dict, List

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
