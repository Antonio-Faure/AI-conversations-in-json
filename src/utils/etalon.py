"""Metriques et controle de regression d'une conversation d'etalonnage.

L'etalonnage sert de canari : si le site change son DOM, le parseur perd des
capacites (images, tableaux, listes, langues de code, LaTeX...) et l'alternance
des roles peut casser. On resume ces signaux pour comparer dans le temps.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT / "scripts"


def etalons_file(path: Optional[Union[Path, str]] = None) -> Path:
    """Fichier d'etalons : chemin explicite, sinon local, sinon exemple.

    `scripts/etalons.json` contient les ids reels des conversations (non
    versionne) ; `scripts/etalons.example.json` sert de modele.
    """
    if path is not None:
        return Path(path)
    local = SCRIPTS_DIR / "etalons.json"
    return local if local.exists() else SCRIPTS_DIR / "etalons.example.json"


def load_etalons(path: Optional[Union[Path, str]] = None) -> Dict[str, Any]:
    """Charge la configuration des etalons (locale ou exemple)."""
    return json.loads(etalons_file(path).read_text(encoding="utf-8"))

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


#: motifs de fichiers joints -> capacite (tolerance : suffixe « (1) », autre extension)
_FILE_PATTERNS = (
    (r"document[^ \n]*\.pdf", "upload_pdf"),
    (r"donnees[^ \n]*\.csv", "upload_csv"),
    (r"donnees[^ \n]*\.json", "upload_json"),
    (r"notes[^ \n]*\.txt", "upload_text"),
    (r"notes[^ \n]*\.md", "upload_text"),
    (r"audio[^ \n]*\.(?:mp3|wav|m4a|ogg)", "audio"),
    (r"video[^ \n]*\.(?:mp4|mov|webm)", "video"),
    (r"image[^ \n]*\.(?:png|jpe?g|webp|gif)", "upload_image"),
    (r"\]\(images/", "upload_image"),
)

#: capacites verifiables automatiquement depuis un export (JSON + HTML optionnel)
DETECTABLE = frozenset(_DETECTORS) | {
    "code_block", "code_languages", "code_long", "long_message",
    "upload_image", "upload_pdf", "upload_csv", "upload_json", "upload_text",
    "audio", "video", "reasoning", "artifact_canvas",
    "web_search_citations", "code_execution", "connectors",
    "image_generation", "thread_memory",
}

#: mot-cle dans Message.tools -> capacite (comparaison en minuscules)
_TOOL_CAPS = (
    ("code_execution", ("exécution de code", "code", "terminal", "commande", "python")),
    ("connectors", ("connecteur", "mcp")),
    ("image_generation", ("génération d'image", "génération d’image")),
    ("thread_memory", ("mémoire",)),
)


def detect_capabilities(payload: Dict[str, Any], html: str = "") -> Set[str]:
    """Capacites observables dans un export scraped (JSON + HTML optionnel).

    `html` (page sauvegardee) sert aux capacites non presentes dans le texte
    (lecteurs audio/video, etc.).
    """
    messages = payload.get("messages") or []
    texts = [str(m.get("texte") or "") for m in messages]
    joined = "\n".join(texts)
    lowered = joined.lower()
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
    for pattern, capability in _FILE_PATTERNS:
        if re.search(pattern, lowered):
            found.add(capability)
    if any((m.get("reasoning") or "").strip() for m in messages):
        found.add("reasoning")
    if any(m.get("artifacts") for m in messages):
        found.add("artifact_canvas")
    if any(m.get("sources") for m in messages):
        found.add("web_search_citations")
    tool_labels = " ".join(
        str(tool).lower() for m in messages for tool in (m.get("tools") or [])
    )
    for capability, keywords in _TOOL_CAPS:
        if any(keyword in tool_labels for keyword in keywords):
            found.add(capability)
    if html:
        page = html.lower()
        if "audio.mp3" in page or "<audio" in page:
            found.add("audio")
        if "video.mp4" in page or "<video" in page:
            found.add("video")
    return found


def normalize_for_match(text: str, limit: int = 25) -> str:
    """Cle de rapprochement d'un message : alphanumerique pur, tronque.

    Insensible aux accents, espaces et ponctuation (les clients rerendent le
    markdown differemment d'une conversation a l'autre).
    """
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    return "".join(c for c in ascii_only.lower() if c.isalnum())[:limit]


def queue_coverage(
    queue_texts: List[str], export_user_texts: List[str]
) -> Dict[str, List[int]]:
    """Indices (1-based) de la file presents dans l'export, et manquants.

    Appariement un-a-un par similarite (les clients rerendent le markdown
    differemment : URL d'image remplacee, espaces, troncatures).
    """
    from difflib import SequenceMatcher

    keys_short = [normalize_for_match(t, limit=25) for t in queue_texts]
    keys_long = [normalize_for_match(t, limit=120) for t in queue_texts]
    seen_short = [normalize_for_match(t, limit=25) for t in export_user_texts]
    seen_long = [normalize_for_match(t, limit=120) for t in export_user_texts]
    used: set = set()
    present = set()
    for index, key in enumerate(keys_short, start=1):
        if not key:
            continue
        candidates = [
            j for j, candidate in enumerate(seen_short)
            if j not in used and candidate and (key in candidate or candidate in key)
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            best_j = candidates[0]
        else:
            best_j = max(
                candidates,
                key=lambda j: SequenceMatcher(None, keys_long[index - 1], seen_long[j]).ratio(),
            )
        present.add(index)
        used.add(best_j)
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
