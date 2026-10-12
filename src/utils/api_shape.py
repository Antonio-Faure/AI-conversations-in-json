"""Forme canonique (value-free) des payloads des API internes.

Un payload JSON est reduit a un arbre canonique decrivant **seulement sa
structure** : chemins, types (`str`/`int`/`float`/`bool`/`null`/`list`/`object`),
cardinalite des listes (bucketee : 0, 1, 2-5, 6-20, 21-100, 100+) et **tags de
format** des chaines (uuid, iso-date, url, hash, email). Aucune valeur scalaire
du payload n'apparait dans la sortie : les tags sont calcules (une URL devient
le tag `url`), jamais recopies.

Comparaison (`compare_shapes`) : la reference est un **vocabulaire de formes**
(union de plusieurs payloads, cf. `merge_shapes`). Un payload live est score
par sa **compatibilite** avec ce vocabulaire :

  - structure (60 %) : part des chemins du live connus de la reference avec un
    type compatible (un chemin inconnu = structure nouvelle -> penalise) ;
  - types (25 %) : part des chemins communs dont le type est compatible ;
  - cardinalite (15 %) : part des listes communes dont le bucket n'excede pas
    celui de la reference (une liste plus courte est normale).

Tolerances volontaires (les conversations sont heterogenes) : `null` est
compatible avec tout type, un type compose (`int+str+null`) accepte ses
membres, une liste plus courte que le maximum vu est compatible, un tag de
format deja vu peut disparaitre. Les ecarts restent listes : `added`
(structure inconnue), `removed` (branche non utilisee par ce payload),
`type_changes`, `cardinality_changes`, `format_changes` (nouveau format).

Le moniteur (`scripts/api_bench.py`, `ShapeMonitor`) compare la forme d'un
payload live a une reference versionnee (`tests/fixtures/api_shapes/`).

Module pur : aucune dependance externe, aucune lecture de fichier.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: profondeur maxi de l'arbre (au-dela : noeud marque `cut`)
MAX_DEPTH = 12
#: nombre maxi d'items fusionnes par liste (forme des items)
MAX_LIST_SAMPLES = 200

#: buckets de cardinalite de liste (bornes incluses)
_BUCKETS: Tuple[Tuple[int, int, str], ...] = (
    (0, 0, "0"),
    (1, 1, "1"),
    (2, 5, "2-5"),
    (6, 20, "6-20"),
    (21, 100, "21-100"),
)
_OVERFLOW_BUCKET = "100+"
_BUCKET_LABELS: Tuple[str, ...] = tuple(label for _, _, label in _BUCKETS) + (
    _OVERFLOW_BUCKET,
)

#: tags de format des chaines (calcules, jamais stockes en clair)
_TAG_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "uuid",
        re.compile(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
        ),
    ),
    (
        "iso-date",
        re.compile(
            r"^\d{4}-\d{2}-\d{2}"
            r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
        ),
    ),
    ("url", re.compile(r"^(?:https?|ftp)://\S+$")),
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    (
        "hash",
        re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"),
    ),
)


def bucket_of(count: int) -> str:
    """Bucket de cardinalite d'une liste (`0`, `1`, `2-5`, ... `100+`)."""
    for low, high, label in _BUCKETS:
        if low <= count <= high:
            return label
    return _OVERFLOW_BUCKET


def format_tags(value: str) -> List[str]:
    """Tags de format d'une chaine (liste triee, vide si aucun)."""
    return sorted(name for name, pattern in _TAG_PATTERNS if pattern.match(value))


#: motifs *non ancres* utilises pour masquer une valeur embarquee dans une cle
_KEY_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_KEY_HASH_RE = re.compile(r"[0-9a-fA-F]{32,64}")
_KEY_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def canonical_key(key: Any) -> str:
    """Cle d'objet canonicalisee : une cle qui *est* une valeur est masquee.

    Les dictionnaires indexes par identifiant (uuid, hash, email...) sont
    courants dans les payloads : garder la valeur en clair ferait fuiter une
    donnee du payload. On remplace les motifs de valeur par un placeholder
    type (`<uuid>`, `<email>`, `<hash>`), y compris dans une cle composee
    (`<uuid>:3`), ce qui collapse en plus ces cartes dynamiques.
    """
    text = str(key)
    text = _KEY_UUID_RE.sub("<uuid>", text)
    text = _KEY_EMAIL_RE.sub("<email>", text)
    text = _KEY_HASH_RE.sub("<hash>", text)
    tags = format_tags(text)
    return f"<{tags[0]}>" if tags else text


def shape_of(
    payload: Any,
    *,
    max_depth: int = MAX_DEPTH,
    max_list_samples: int = MAX_LIST_SAMPLES,
) -> Dict[str, Any]:
    """Arbre canonique value-free d'un payload JSON.

    Les cles d'objet sont conservees (noms de champs = structure), sauf celles
    qui sont des valeurs (uuid, hash, email, url, date) : elles sont masquees
    par un placeholder type (`<uuid>`), comme les tags de format des chaines.
    """
    samples = max(1, int(max_list_samples))
    depth = max(1, int(max_depth))
    return _shape(payload, 0, depth, samples)


def signature_of_shape(shape: Dict[str, Any]) -> str:
    """Empreinte SHA-256 stable d'un arbre de forme."""
    return hashlib.sha256(canonical_shape(shape).encode("ascii")).hexdigest()


def signature_of(
    payload: Any,
    *,
    max_depth: int = MAX_DEPTH,
    max_list_samples: int = MAX_LIST_SAMPLES,
) -> str:
    """Empreinte SHA-256 stable de la forme d'un payload."""
    shape = shape_of(payload, max_depth=max_depth, max_list_samples=max_list_samples)
    return signature_of_shape(shape)


def canonical_shape(shape: Dict[str, Any]) -> str:
    """Serialisation canonique d'un arbre de forme (base des signatures)."""
    return json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def flatten_shape(shape: Dict[str, Any], path: str = "$") -> Dict[str, Dict[str, Any]]:
    """Arbre de forme -> table `chemin -> noeud` (paths stables pour le diff).

    Un objet descend en `$.cle`, une liste descend en `$.champ[]` (forme des
    items) puis `$.champ[].cle`. Les noeuds a type compose (`object+str`)
    gardent leurs enfants.
    """
    out: Dict[str, Dict[str, Any]] = {path: shape}
    kinds = types_of(shape)
    if "object" in kinds:
        for key, child in (shape.get("c") or {}).items():
            out.update(flatten_shape(child, f"{path}.{key}"))
    if "list" in kinds:
        item = shape.get("i")
        if isinstance(item, dict):
            out.update(flatten_shape(item, f"{path}[]"))
    return out


def level_for(score: float, threshold: float = 95.0, warn: float = 80.0) -> str:
    """Niveau d'un score : `OK` (>= threshold), `WARN` (>= warn), `BREAK`."""
    if score >= threshold:
        return "OK"
    if score >= warn:
        return "WARN"
    return "BREAK"


def merge_shapes(shapes: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Fusionne plusieurs arbres de forme en un vocabulaire unique (union).

    Les types scalaires divergents deviennent composes (`int+str`), les cles
    d'objet et les tags sont unis, les buckets de liste prennent le maximum.
    L'operation est commutative et associative (ordre indifferent).
    """
    merged: Optional[Dict[str, Any]] = None
    for shape in shapes:
        if not isinstance(shape, dict):
            continue
        merged = shape if merged is None else _merge_nodes([merged, shape])
    return merged


def types_of(node: Dict[str, Any]) -> set[str]:
    """Types d'un noeud (`str` ou compose `int+str+null` -> ensemble)."""
    return {part for part in str(node.get("t") or "").split("+") if part}


def types_compatible(ref_node: Dict[str, Any], live_node: Dict[str, Any]) -> bool:
    """Compatibilite de type entre deux noeuds de forme.

    Compatible si les ensembles de types s'intersectent, ou si l'un des deux
    est/contient `null` (champ optionnel non renseigne).
    """
    ref_types = types_of(ref_node)
    live_types = types_of(live_node)
    if ref_types & live_types:
        return True
    return "null" in ref_types or "null" in live_types


def compare_shapes(ref: Dict[str, Any], live: Dict[str, Any]) -> Dict[str, Any]:
    """Score de compatibilite d'une forme live avec une reference (0-100).

    Ponderation : structure (chemins du live connus de la reference) 60 %,
    types (chemins communs au type compatible) 25 %, cardinalite (listes
    communes sans depassement de bucket) 15 %. `removed` (branches de la
    reference non utilisees par le payload) est informatif : il ne penalise
    pas le score. Les ecarts penalisants (`added`, `type_changes`,
    `cardinality_changes`, `format_changes`) sont listes par chemin.
    """
    ref_flat = flatten_shape(ref)
    live_flat = flatten_shape(live)
    ref_paths, live_paths = set(ref_flat), set(live_flat)
    added = sorted(live_paths - ref_paths)
    removed = sorted(ref_paths - live_paths)
    common = sorted(ref_paths & live_paths)

    compatible: List[str] = []
    type_changes: List[str] = []
    for path in common:
        if types_compatible(ref_flat[path], live_flat[path]):
            compatible.append(path)
        else:
            type_changes.append(path)

    list_paths = [p for p in compatible if "list" in types_of(ref_flat[p])]
    cardinality_changes = [
        p for p in list_paths
        if _bucket_rank(str(live_flat[p].get("n") or "0"))
        > _bucket_rank(str(ref_flat[p].get("n") or "0"))
    ]
    format_changes = [
        p for p in compatible
        if "str" in types_of(ref_flat[p])
        and "str" in types_of(live_flat[p])
        and set(live_flat[p].get("fmt") or []) - set(ref_flat[p].get("fmt") or [])
    ]

    structure = (
        100.0 * len(compatible) / len(live_paths) if live_paths else 100.0
    )
    types = 100.0 * len(compatible) / len(common) if common else 100.0
    cardinality = (
        100.0 * (len(list_paths) - len(cardinality_changes)) / len(list_paths)
        if list_paths
        else 100.0
    )
    score = 0.6 * structure + 0.25 * types + 0.15 * cardinality
    return {
        "score": round(score, 2),
        "level": level_for(score),
        "structure": round(structure, 2),
        "types": round(types, 2),
        "cardinality": round(cardinality, 2),
        "added": added,
        "removed": removed,
        "type_changes": type_changes,
        "cardinality_changes": cardinality_changes,
        "format_changes": format_changes,
        "counts": {
            "ref_paths": len(ref_paths),
            "live_paths": len(live_paths),
            "common": len(common),
        },
    }


# -- construction de l'arbre ---------------------------------------------------


def _shape(
    value: Any, depth: int, max_depth: int, samples: int
) -> Dict[str, Any]:
    """Noeud de forme d'une valeur (recursif)."""
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):
        return {"t": "bool"}
    if isinstance(value, int):
        return {"t": "int"}
    if isinstance(value, float):
        return {"t": "float"}
    if isinstance(value, str):
        node: Dict[str, Any] = {"t": "str"}
        tags = format_tags(value)
        if tags:
            node["fmt"] = tags
        return node
    if isinstance(value, dict):
        if depth >= max_depth:
            return {"t": "object", "c": {}, "cut": True}
        return {
            "t": "object",
            "c": {
                canonical_key(key): _shape(item, depth + 1, max_depth, samples)
                for key, item in sorted(
                    value.items(), key=lambda kv: canonical_key(kv[0])
                )
            },
        }
    if isinstance(value, (list, tuple)):
        count = len(value)
        node = {"t": "list", "n": bucket_of(count)}
        if depth >= max_depth:
            node["cut"] = True
            return node
        if count:
            # fusion incrementale des items (visite jusqu'a `samples` items) :
            # les branches rares ne sont pas perdues par un echantillon fixe.
            item: Optional[Dict[str, Any]] = None
            for element in _sample_items(value, samples):
                part = _shape(element, depth + 1, max_depth, samples)
                item = part if item is None else _merge_nodes([item, part])
            if item is not None:
                node["i"] = item
        return node
    # type inattendu (bytes, objet python...) : type brut, jamais la valeur
    return {"t": type(value).__name__}


def _sample_items(items: Sequence[Any], samples: int) -> List[Any]:
    """Echantillonne au plus `samples` items repartis sur la liste."""
    count = len(items)
    if count <= samples:
        return list(items)
    return [items[int(index * count / samples)] for index in range(samples)]


def _merge_nodes(nodes: Sequence[Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Fusionne les formes d'items heterogenes en un noeud unique.

    Les types divergents deviennent composes (`int+str`) ; les enfants d'objet,
    les items de liste et les tags sont unis (le type compose garde la
    structure des branches objet/liste rencontrees).
    """
    present = [node for node in nodes if isinstance(node, dict)]
    if not present:
        return None
    kinds = sorted({kind for node in present for kind in types_of(node)})
    if len(kinds) == 1 and kinds[0] == "object":
        return _merge_object_nodes(present)
    if len(kinds) == 1 and kinds[0] == "list":
        return _merge_list_nodes(present)
    merged: Dict[str, Any] = {"t": "+".join(kinds)}
    object_nodes = [node for node in present if "object" in types_of(node)]
    if object_nodes:
        object_merged = _merge_object_nodes(object_nodes)
        merged["c"] = object_merged.get("c") or {}
    list_nodes = [node for node in present if "list" in types_of(node)]
    if list_nodes:
        list_merged = _merge_list_nodes(list_nodes)
        merged["n"] = list_merged.get("n") or "0"
        if list_merged.get("i") is not None:
            merged["i"] = list_merged["i"]
    tags = sorted(
        {str(tag) for node in present for tag in (node.get("fmt") or [])}
    )
    if tags:
        merged["fmt"] = tags
    if all(node.get("cut") for node in present):
        merged["cut"] = True
    return merged


def _merge_object_nodes(
    nodes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fusion d'un ensemble de noeuds objet (union des cles)."""
    children: Dict[str, Any] = {}
    keys = sorted(
        {str(key) for node in nodes for key in (node.get("c") or {})}
    )
    for key in keys:
        children[key] = _merge_nodes(
            [
                (node.get("c") or {}).get(key)
                for node in nodes
                if key in (node.get("c") or {})
            ]
        )
    merged: Dict[str, Any] = {"t": "object", "c": children}
    if all(node.get("cut") for node in nodes):
        merged["cut"] = True
    return merged


def _merge_list_nodes(
    nodes: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Fusion d'un ensemble de noeuds liste (bucket maxi, items unis)."""
    merged: Dict[str, Any] = {
        "t": "list",
        "n": max(
            (str(node.get("n") or "0") for node in nodes), key=_bucket_rank
        ),
    }
    item = _merge_nodes([node.get("i") for node in nodes])
    if item is not None:
        merged["i"] = item
    if all(node.get("cut") for node in nodes):
        merged["cut"] = True
    return merged


def _bucket_rank(label: str) -> int:
    """Rang d'un bucket (pour choisir le plus grand lors d'une fusion)."""
    try:
        return _BUCKET_LABELS.index(label)
    except ValueError:
        return len(_BUCKET_LABELS)
