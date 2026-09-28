"""Screenshot d'un message : centrage d'un tour DOM puis capture du viewport.

Les fils longs sont virtualises (seule une fenetre de tours est montee dans le
DOM) : un simple index sur la fenetre courante plafonne et duplique les
captures. On avance donc de maniere monotone du haut vers le bas, en
memorisant l'identite de chaque tour deja capture (attribut du DOM + debut du
texte), et on fait défiler le conteneur pour charger les tours suivants.

Le JS prend en priorite les elements externes (les tours imbriques - ex.
ChatGPT `conversation-turn` contenant `[data-message-author-role]` - sont
dedupliques) et ignore les tours masques (display:none / taille nulle).
"""

from __future__ import annotations

import json
from typing import Iterable, List

#: nombre max de tours captures (garde-fou)
MAX_TURNS = 300
#: nombre max d'iterations JS (captures + avancees du scroll)
MAX_STEPS = MAX_TURNS * 4
#: pause apres un centrage / un defilement (rendu, images)
SETTLE_MS = 400
#: rounds sans nouvelle capture avant de conclure que le fil est termine
STABLE_ROUNDS = 4

RESET_BODY = r"""
return (function () {
  const sels = __SELECTORS__;
  let first = null;
  for (const s of sels) {
    try { const n = document.querySelector(s); if (n) { first = n; break; } } catch (e) {}
  }
  let el = first;
  while (el) {
    if (el.scrollHeight > el.clientHeight + 4) { el.scrollTop = 0; }
    el = el.parentElement;
  }
  try { (document.scrollingElement || document.documentElement).scrollTop = 0; } catch (e) {}
  try { window.scrollTo(0, 0); } catch (e) {}
  window.__aicvShots = {seen: []};
  return !!first;
})();
"""

STEP_BODY = r"""
return (function () {
  const sels = __SELECTORS__;
  const st = window.__aicvShots || (window.__aicvShots = {seen: []});
  const seen = new Set(st.seen);
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
    let nested = false;
    for (const k of kept) { if (k.contains(n)) { nested = true; break; } }
    if (!nested) kept.push(n);
  }
  const idOf = function (n) {
    const attrs = ['data-message-id', 'data-turn-id', 'data-testid', 'id'];
    let id = '';
    for (const k of attrs) {
      const v = n.getAttribute ? n.getAttribute(k) : null;
      if (v) { id = k + ':' + v; break; }
    }
    const text = (n.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    return id + '|' + text;
  };
  for (const n of kept) {
    if (!seen.has(idOf(n))) {
      n.scrollIntoView({block: 'center', inline: 'nearest'});
      st.seen.push(idOf(n));
      return 'shot';
    }
  }
  const last = kept[kept.length - 1];
  if (!last) return 'done';
  last.scrollIntoView({block: 'end', inline: 'nearest'});
  return 'advance';
})();
"""


def _inject(selectors: Iterable[str], body: str) -> str:
    return body.replace("__SELECTORS__", json.dumps(list(selectors)))


def screenshot_reset_body(selectors: Iterable[str]) -> str:
    """Corps JS `return ...` : remonte en haut du fil et reinitialise l'etat."""
    return _inject(selectors, RESET_BODY)


def screenshot_step_body(selectors: Iterable[str]) -> str:
    """Corps JS `return ...` : 'shot' (nouveau tour centre), 'advance' (charger
    la suite) ou 'done' (plus aucun tour monte)."""
    return _inject(selectors, STEP_BODY)


def message_screenshot_names(count: int) -> List[str]:
    return [f"message-{i + 1:02d}.png" for i in range(count)]
