"""Construction de la conversation d'etalonnage : extraction et compactage.

Les chatbots generent eux-memes une « suite complete de messages utilisateur »
pour tester leurs capacites. On extrait ces tests, on les tague par capacite,
puis on selectionne le plus petit ensemble couvrant tout (set-cover glouton).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: amorce qui declenche la generation de la suite
BOOTSTRAP_SUITE = "suite complète de messages utilisateur"
#: amorce de documentation des capacites
BOOTSTRAP_DOC = "Dresse une documentation complète"

_SEC_RE = re.compile(r"(?m)^\s*Capacit[ée] test[ée]e\s*:\s*(.*)$")
_PREP_RE = re.compile(r"(?m)^\s*Pr[ée]paration\s*:\s*(.*)$")
_MSG_RE = re.compile(
    r"Message utilisateur à envoyer[^\n]*\n+(?:```[a-zA-Z0-9]*[ \t]*\n)(.*?)```", re.S
)
#: repli quand les fences ne sont pas dans le texte (canvas HTML rendu en <pre>)
_MSG_FALLBACK_RE = re.compile(
    r"Message utilisateur à envoyer[^\n]*\n(.*?)(?=\n\s*R[ée]sultat attendu|\Z)", re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_HEAD_RE = re.compile(r"(?m)^\s*#{1,6}\s+(.+?)\s*$")

#: sections de domaine fort -> categorie unique (les autres n'imposent pas de filtre,
#: car mise en forme / listes / tableaux / code se recouvrent trop pour trancher).
_SECTION_RULES = (
    ("latex", frozenset({"math"})),
    ("math", frozenset({"math"})),
    ("equation", frozenset({"math"})),
    ("équation", frozenset({"math"})),
    # "generation" avant "fichier" : « Generation de fichiers » = generation
    ("canvas", frozenset({"generation"})),
    ("artefact", frozenset({"generation"})),
    ("artifact", frozenset({"generation"})),
    ("generation", frozenset({"generation"})),
    ("image", frozenset({"media"})),
    ("fichier", frozenset({"media"})),
    ("audio", frozenset({"media"})),
    ("video", frozenset({"media"})),
    ("vidéo", frozenset({"media"})),
    ("media", frozenset({"media"})),
    ("recherche", frozenset({"tools"})),
    ("web", frozenset({"tools"})),
    ("outil", frozenset({"tools"})),
)


def section_categories(section: str) -> Optional[frozenset]:
    """Categories autorisees par le titre de section (None = pas de filtre)."""
    folded = fold(section)
    for needle, categories in _SECTION_RULES:
        if needle in folded:
            return categories
    return None


def _is_test_heading(title: str) -> bool:
    """Titre d'un test individuel (« Test 1.2 — ... ») : pas un contexte."""
    return re.match(r"test\b", fold(title).strip()) is not None


_BRACKET_ONLY_RE = re.compile(r"\s*\[[^\]]{0,80}\]\s*")


def _is_placeholder(*texts: str) -> bool:
    """Modele/template (« [message exact a copier-coller] ») : a ignorer."""
    for text in texts:
        folded = fold(text)
        if "message exact a copier" in folded or "copier-coller" in folded:
            return True
        if _BRACKET_ONLY_RE.fullmatch(text or ""):
            return True
    return False


def _prompt_from_body(body: str) -> str:
    """Message utilisateur d'un test : bloc fenced, sinon texte avant le resultat."""
    match = _MSG_RE.search(body)
    if match:
        return match.group(1).strip()
    fallback = _MSG_FALLBACK_RE.search(body)
    if not fallback:
        return ""
    raw = re.sub(r"```[a-zA-Z0-9]*", "", fallback.group(1))
    return raw.strip()


def fold(text: str) -> str:
    """Minuscule sans accents (comparaison robuste des mots-cles)."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    return ascii_only.lower()


def normalize(text: str) -> str:
    """Nettoie le markdown et les espaces insecables avant extraction."""
    text = (text or "").replace("\u00a0", " ").replace("\u200b", "")
    return text.replace("**", "")


def html_to_text(html: str) -> str:
    """Texte brut d'un HTML (canvas Mistral) : sauts de ligne preserves."""
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|span|pre|code|h[1-6])>", "\n", html)
    import html as _html

    return _html.unescape(_TAG_RE.sub("", html))


@dataclass
class CalibrationTest:
    index: int
    capability: str
    preparation: str
    prompt: str
    section: str = ""
    capabilities: Set[str] = field(default_factory=set)
    attachments: List[str] = field(default_factory=list)


def extract_tests(suite_text: str) -> List[CalibrationTest]:
    """Extrait les tests {section, capacite, preparation, message a envoyer}."""
    text = normalize(suite_text)
    headings = [(m.start(), " ".join(m.group(1).split())) for m in _HEAD_RE.finditer(text)]
    starts = list(_SEC_RE.finditer(text))
    tests: List[CalibrationTest] = []
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        body = text[match.end():end]
        prompt = _prompt_from_body(body)
        if not prompt:
            continue
        capability = " ".join(match.group(1).split())
        if _is_placeholder(prompt, capability):
            continue
        prep_match = _PREP_RE.search(body)
        preparation = " ".join(prep_match.group(1).split()) if prep_match else ""
        section = ""
        for pos, title in headings:
            if pos >= match.start():
                break
            if _is_test_heading(title):
                continue
            section = title
        tests.append(
            CalibrationTest(
                index=len(tests),
                capability=capability,
                preparation=preparation,
                prompt=prompt,
                section=section,
            )
        )
    return tests


def load_manifest(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _capability_index(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(manifest.get("capabilities") or [])


def tag_tests(tests: Sequence[CalibrationTest], manifest: Dict[str, Any]) -> None:
    """Renseigne `capabilities` (ids du manifeste) sur chaque test."""
    rules = [
        (c["id"], c.get("category"), [fold(k) for k in c.get("keywords") or []])
        for c in _capability_index(manifest)
    ]
    media = {fold(k): v for k, v in (manifest.get("media") or {}).items()}
    for test in tests:
        allowed = section_categories(test.section)
        haystack = fold(f"{test.capability} {test.prompt} {test.preparation}")
        test.capabilities = {
            cid
            for cid, category, keys in rules
            if (allowed is None or category in allowed) and any(k in haystack for k in keys)
        }
        test.attachments = _attachments(test.preparation, test.prompt, media)


#: indices d'un fichier fourni par l'utilisateur (pour deduire la piece jointe)
_UPLOAD_HINTS = (
    "fichier joint", "fichier ci-joint", "ci-joint", "ci joint", "ce fichier",
    "cette image", "image jointe", "capture d'ecran", "capture ecran", "ce document",
    "cet audio", "cette video", "cette vidéo", "cet enregistrement", "jointe", "joint",
)
#: mots supplementaires -> fichier du jeu standard
_EXTRA_ALIASES = {
    "capture": "image.png", "capture d'ecran": "image.png", "screenshot": "image.png",
    "photo": "image.png", "enregistrement": "audio.mp3", "transcription": "audio.mp3",
    "voix": "audio.mp3",
}


def _media_files(text: str, media: Dict[str, str]) -> List[str]:
    folded = fold(text)
    found = {name for key, name in media.items() if key in folded}
    found.update(name for key, name in _EXTRA_ALIASES.items() if fold(key) in folded)
    return sorted(found)


def _attachments(preparation: str, prompt: str, media: Dict[str, str]) -> List[str]:
    prep = fold(preparation)
    if prep and not prep.startswith("aucune"):
        return _media_files(preparation, media)
    if any(hint in fold(prompt) for hint in _UPLOAD_HINTS):
        return _media_files(prompt, media)
    return []


def dedupe(tests: Sequence[CalibrationTest]) -> List[CalibrationTest]:
    """Retire les tests dont le message est identique (normalise)."""
    seen: Set[str] = set()
    kept: List[CalibrationTest] = []
    for test in tests:
        key = " ".join(fold(test.prompt).split())
        if not key or key in seen:
            continue
        seen.add(key)
        test.index = len(kept)
        kept.append(test)
    return kept


def select_minimal_cover(
    tests: Sequence[CalibrationTest], required: Iterable[str]
) -> Tuple[List[CalibrationTest], Set[str]]:
    """Set-cover glouton : plus petit ensemble couvrant `required`.

    Retourne (tests retenus dans l'ordre d'origine, capacites non couvertes).
    """
    remaining = set(required)
    selected: List[CalibrationTest] = []
    while remaining:
        best: Optional[CalibrationTest] = None
        best_gain: Set[str] = set()
        for test in tests:
            gain = test.capabilities & remaining
            if len(gain) > len(best_gain):
                best, best_gain = test, gain
        if best is None or not best_gain:
            break
        selected.append(best)
        remaining -= best_gain
    selected.sort(key=lambda t: t.index)
    return selected, remaining


def build_payload(
    platform: str,
    tests: Sequence[CalibrationTest],
    manifest: Dict[str, Any],
    mode: Optional[str] = None,
    source_conversation: Optional[str] = None,
    generated_from: Optional[str] = None,
) -> Dict[str, Any]:
    """Construit le fichier de calibration d'un bot (messages compactes)."""
    tests = dedupe(tests)
    tag_tests(tests, manifest)
    all_ids = [c["id"] for c in _capability_index(manifest)]
    coverable = {cid for test in tests for cid in test.capabilities}
    required = coverable & set(all_ids)
    selected, gaps = select_minimal_cover(tests, required)
    covered = {cid for test in selected for cid in test.capabilities}
    messages = [
        {
            "text": test.prompt,
            "attachments": test.attachments,
            "exposes": sorted(test.capabilities),
            "source_test": test.capability,
        }
        for test in selected
    ]
    return {
        "platform": platform,
        "mode": mode,
        "source_conversation": source_conversation,
        "generated_from": generated_from,
        "test_pool": len(tests),
        "messages": messages,
        "capabilities_covered": [cid for cid in all_ids if cid in covered],
        "capabilities_coverable_uncovered": sorted(gaps),
        "capabilities_missing": [cid for cid in all_ids if cid not in coverable],
    }


def suite_from_export(payload: Dict[str, Any]) -> Optional[str]:
    """Retourne le texte de la suite generee dans un export JSON, sinon None."""
    messages = payload.get("messages") or []
    for i, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        if BOOTSTRAP_SUITE in (message.get("texte") or ""):
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                return messages[i + 1].get("texte") or ""
    return None


def suite_from_path(kind: str, path: Path) -> Optional[str]:
    """Charge la suite depuis un export JSON ou un HTML (canvas Mistral)."""
    path = Path(path)
    if kind == "html":
        return html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    if kind in ("md", "markdown", "txt"):
        return path.read_text(encoding="utf-8", errors="replace")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return suite_from_export(payload)


def bootstraps_from_export(payload: Dict[str, Any]) -> Dict[str, str]:
    """Recupere les deux amorces (documentation + generation de suite)."""
    doc = suite = ""
    for message in payload.get("messages") or []:
        text = message.get("texte") or ""
        if message.get("role") != "user":
            continue
        if not doc and BOOTSTRAP_DOC in text:
            doc = text.strip()
        if not suite and BOOTSTRAP_SUITE in text:
            suite = text.strip()
    return {"documentation": doc, "suite": suite}
