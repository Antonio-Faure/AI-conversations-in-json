"""Traduction des selecteurs Playwright vers ce que botasaurus_driver comprend.

botasaurus_driver (CDP pur) ne connait que le CSS standard : ni `text=...`,
ni `:has-text()`, ni `:text-is()`, ni XPath. Ce module decoupe un selecteur
Playwright en (css, predicat de texte) et fournit les builders JS pour
evaluer le predicat cote navigateur.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Sel:
    """Selecteur traduit : css + predicat de texte optionnel.

    css   : selecteur CSS standard (None si selecteur purement textuel)
    text  : sous-chaine insensible a la casse (equiv. :has-text)
    exact : texte exact (equiv. :text-is)
    regex : motif applique au texte de la page (equiv. text=/re/flags)
    """

    css: Optional[str] = None
    text: Optional[str] = None
    exact: Optional[str] = None
    regex: Optional[str] = None

    @property
    def plain(self) -> bool:
        return self.css is not None and self.text is None and self.exact is None and self.regex is None


_QUOTED = r"(\"([^\"]*)\"|'([^']*)')"


def _quoted_value(match: re.Match) -> str:
    return match.group(2) if match.group(2) is not None else match.group(3)


def translate_selector(selector: str) -> Sel:
    """`button:has-text('Log in')` -> Sel(css='button', text='Log in'), etc.

    Le CSS standard passe tel quel (y compris `[attr*='x' i]`, supporte par
    Chrome). Les pseudo-classes Playwright non traduites sont considerees
    comme du CSS simple : le service devra les eviter pour le moteur bota.
    """
    s = selector.strip()
    m = re.match(r"^text\s*=\s*/(.+)/([a-z]*)$", s, re.DOTALL)
    if m:
        return Sel(regex=m.group(1))
    m = re.match(rf"^text\s*=\s*{_QUOTED}$", s)
    if m:
        return Sel(text=_quoted_value(m))
    m = re.search(rf":has-text\(\s*{_QUOTED}\s*\)", s)
    if m:
        return Sel(css=s[: m.start()].strip() or None, text=_quoted_value(m))
    m = re.search(rf":text-is\(\s*{_QUOTED}\s*\)", s)
    if m:
        return Sel(css=s[: m.start()].strip() or None, exact=_quoted_value(m))
    return Sel(css=s)


def js_presence_body(sel: Sel) -> Optional[str]:
    """JS (style run_js, avec return) testant la presence du selecteur.

    None si le selecteur est du CSS simple (utiliser is_element_present).
    """
    if sel.regex is not None:
        return (
            "return new RegExp(" + json.dumps(sel.regex) + ")"
            ".test(document.body ? (document.body.innerText || '') : '');"
        )
    if sel.css is None:
        return "return false;"
    if sel.text is not None:
        return (
            "const els = document.querySelectorAll(" + json.dumps(sel.css) + ");"
            "const needle = " + json.dumps(sel.text.lower()) + ";"
            "for (const el of els) {"
            "  const t = (el.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();"
            "  if (t.includes(needle)) return true;"
            "} return false;"
        )
    if sel.exact is not None:
        return (
            "const els = document.querySelectorAll(" + json.dumps(sel.css) + ");"
            "const needle = " + json.dumps(sel.exact) + ";"
            "for (const el of els) {"
            "  const t = (el.textContent || '').trim();"
            "  if (t === needle) return true;"
            "} return false;"
        )
    return None


def js_click_body(sel: Sel) -> Optional[str]:
    """JS cliquant le premier element correspondant. None si CSS simple."""
    if sel.css is None:
        return None
    base = (
        "const els = document.querySelectorAll(" + json.dumps(sel.css) + ");"
        "for (const el of els) {"
    )
    if sel.text is not None:
        base += (
            "  const t = (el.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();"
            "  if (!t.includes(" + json.dumps(sel.text.lower()) + ")) continue;"
        )
    elif sel.exact is not None:
        base += (
            "  const t = (el.textContent || '').trim();"
            "  if (t !== " + json.dumps(sel.exact) + ") continue;"
        )
    base += "  el.click(); return true; } return false;"
    return base


def js_scroll_body(sel: Sel) -> Optional[str]:
    """JS scrolant un conteneur et retournant [scrollHeight, at_bottom]."""
    if sel.css is None:
        return None
    find = "const el = document.querySelector(" + json.dumps(sel.css) + ");"
    if not sel.plain:
        find = "const els = document.querySelectorAll(" + json.dumps(sel.css) + "); let el = null;"
        if sel.text is not None:
            find += (
                "for (const e of els) {"
                "  const t = (e.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();"
                "  if (t.includes(" + json.dumps(sel.text.lower()) + ")) { el = e; break; } }"
            )
        elif sel.exact is not None:
            find += (
                "for (const e of els) {"
                "  if ((e.textContent || '').trim() === " + json.dumps(sel.exact) + ") { el = e; break; } }"
            )
    return (
        find
        + "if (!el) return null;"
        + "el.scrollBy({top: el.clientHeight || 400, behavior: 'instant'});"
        + "return [el.scrollHeight, el.scrollTop + el.clientHeight >= el.scrollHeight - 8];"
    )


def playwright_js_to_iife(script: str) -> str:
    """Convertit une fleche Playwright `() => ...` en corps JS pour run_js.

    botasaurus run_js enveloppe le script dans une IIFE : il attend des
    instructions + `return`, pas une fonction.
      - `() => { instructions }` : bloc, `return` deja present -> corps brut
      - `() => expression`       : expression -> `return expression;`
    Les scripts sans fleche (deja en style `return ...`) passent tels quels.
    """
    s = script.strip()
    m = re.match(r"^(?:async\s+)?(?:\(\s*[^)]*\s*\)|[A-Za-z_$][\w$]*)\s*=>\s*", s)
    if not m:
        return s
    body = s[m.end() :].strip()
    if body.startswith("{") and body.endswith("}"):
        return body[1:-1].strip()
    return f"return {body};" if body else ""
