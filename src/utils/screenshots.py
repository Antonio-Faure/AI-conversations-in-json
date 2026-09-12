"""Screenshot d'un message : centrage d'un tour DOM puis capture du viewport.

Le JS prend en priorite les elements externes (les tours imbriques - ex.
ChatGPT `conversation-turn` contenant `[data-message-author-role]` - sont
dedupliques) et ignore les tours masques (display:none / taille nulle).
"""

from __future__ import annotations

import json
from typing import Iterable, List

#: nombre max de tours captures (garde-fou)
MAX_TURNS = 300
#: pause apres le centrage (rendu, images)
SETTLE_MS = 400

SCROLL_BODY = """
const sels = __SELECTORS__;
const index = __INDEX__;
const set = new Set();
for (const s of sels) {
  try { document.querySelectorAll(s).forEach(function (n) { set.add(n); }); } catch (e) {}
}
const visible = function (n) {
  const r = n.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
};
const nodes = Array.from(set).filter(visible).sort(function (a, b) {
  return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING) ? 1 : -1;
});
const kept = [];
for (const n of nodes) {
  let contained = false;
  for (const k of kept) { if (k.contains(n)) { contained = true; break; } }
  if (!contained) kept.push(n);
}
const el = kept[index];
if (!el) return false;
el.scrollIntoView({ block: "center", inline: "nearest" });
return true;
"""


def scroll_to_index_body(selectors: Iterable[str], index: int) -> str:
    """Corps JS `return ...` : centre le tour `index` et retourne s'il existe."""
    return SCROLL_BODY.replace("__SELECTORS__", json.dumps(list(selectors))).replace(
        "__INDEX__", str(int(index))
    )


def message_screenshot_names(count: int) -> List[str]:
    return [f"message-{i + 1:02d}.png" for i in range(count)]
