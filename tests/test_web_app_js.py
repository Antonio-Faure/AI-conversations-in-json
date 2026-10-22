"""Test comportemental du rendu markdown web (web/app.js) via Node.js.

Verifie le repli fail-closed : si DOMPurify n'est pas charge (CDN bloque),
aucun HTML produit par `marked` n'est injecte ; le texte est echappe.
Node est requis (test ignore sinon).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "web" / "app.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node absent")

#: charge le script dans un DOM minimal puis teste `renderMarkdown`
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");

const sandbox = {
  console,
  document: {
    addEventListener() {},
    documentElement: { hasAttribute: () => false, setAttribute() {} },
    getElementById: () => null,
  },
  window: { addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {},
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const XSS = '<img src=x onerror="window.__xss=1">';

// cas 1 : marked charge mais DOMPurify bloque (CDN partiel) -> repli echappe
sandbox.marked = { parse: () => XSS };
const out1 = sandbox.renderMarkdown("charge : " + XSS);
const leaked1 = out1.includes("<img");  // balise reelle, pas le texte echappe

// cas 2 : DOMPurify present -> la sortie de marked est sanitizee
let sanitized = false;
sandbox.DOMPurify = {
  sanitize: (html) => { sanitized = true; return "SANE"; },
};
const out2 = sandbox.renderMarkdown("charge : " + XSS);

// cas 3 : marked absent -> repli texte echappe
delete sandbox.marked;
const out3 = sandbox.renderMarkdown(XSS);
const leaked3 = out3.includes("<img");

console.log(JSON.stringify({ out1, leaked1, sanitized, out2, leaked3 }));
"""


def _run():
    result = subprocess.run(
        [NODE, "-e", HARNESS, str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


def test_repli_fail_closed_sans_dompurify():
    data = _run()
    # sans DOMPurify : aucune balise de la charge utile n'est injectee
    assert data["leaked1"] is False
    assert "&lt;img" in data["out1"]
    # sans marked non plus : meme repli echappe
    assert data["leaked3"] is False
    # avec DOMPurify : la sortie de marked passe bien par sanitize
    assert data["sanitized"] is True
    assert data["out2"] == "SANE"


def test_libs_front_vendorisees():
    """Les bibliotheques front sont servies localement (aucun CDN tiers)."""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr" not in html
    for name in ("marked.min.js", "purify.min.js", "katex.min.js",
                 "auto-render.min.js", "katex.min.css"):
        assert f"vendor/{name}" in html, name
        assert (ROOT / "web" / "vendor" / name).is_file(), name
    # la version de DOMPurify doit etre a jour (CVE mXSS des 3.1.x)
    purify = (ROOT / "web" / "vendor" / "purify.min.js").read_text(encoding="utf-8")
    assert "DOMPurify 3.4" in purify or "DOMPurify 3.5" in purify
    assert (ROOT / "web" / "vendor" / "fonts").is_dir()
