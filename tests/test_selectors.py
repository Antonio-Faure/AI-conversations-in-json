"""Tests du traducteur de selecteurs Playwright -> CSS (moteur botasaurus)."""

from __future__ import annotations

from src.selectors import (
    Sel,
    js_click_body,
    js_presence_body,
    js_scroll_body,
    playwright_js_to_iife,
    translate_selector,
)


class TestTranslateSelector:
    def test_css_simple_tel_que(self):
        assert translate_selector("div.sidebar > a[href*='/c/']") == Sel(
            css="div.sidebar > a[href*='/c/']"
        )

    def test_has_text_extrait_css_et_texte(self):
        sel = translate_selector("button:has-text('Show all')")
        assert sel.css == "button"
        assert sel.text == "Show all"
        assert not sel.plain

    def test_has_text_double_quotes(self):
        sel = translate_selector('nav a:has-text("Log in")')
        assert sel.css == "nav a"
        assert sel.text == "Log in"

    def test_has_text_css_vide(self):
        sel = translate_selector(":has-text('x')")
        assert sel.css is None
        assert sel.text == "x"

    def test_text_is(self):
        sel = translate_selector("span:text-is('Recent')")
        assert sel.css == "span"
        assert sel.exact == "Recent"

    def test_text_regex(self):
        sel = translate_selector("text=/verify you are human/i")
        assert sel.css is None
        assert sel.regex == "verify you are human"

    def test_text_chaine(self):
        sel = translate_selector('text="Just a moment"')
        assert sel.text == "Just a moment"
        assert sel.regex is None

    def test_attribut_insensible_casse_reste_css(self):
        sel = translate_selector("a[href*='login' i]")
        assert sel.plain
        assert sel.css == "a[href*='login' i]"


class TestJsBuilders:
    def test_presence_css_simple_retourne_none(self):
        assert js_presence_body(translate_selector("aside")) is None

    def test_presence_has_text(self):
        body = js_presence_body(translate_selector("button:has-text('Show all')"))
        assert "querySelectorAll" in body
        assert "show all" in body
        assert "return true" in body

    def test_presence_regex_utilise_innerText(self):
        body = js_presence_body(translate_selector("text=/just a moment/i"))
        assert "RegExp" in body
        assert "innerText" in body

    def test_click_has_text_clique_premier_match(self):
        body = js_click_body(translate_selector("button:has-text('Sign in')"))
        assert "el.click()" in body
        assert "sign in" in body

    def test_scroll_retourne_hauteur_et_bottom(self):
        body = js_scroll_body(translate_selector("mat-nav-list"))
        assert "scrollBy" in body
        assert "scrollHeight" in body

    def test_scroll_a_texte(self):
        body = js_scroll_body(translate_selector("aside:has-text('Recent')"))
        assert "recent" in body


class TestPlaywrightJsToIife:
    def test_fleche_bloc_convertie(self):
        js = playwright_js_to_iife("() => { const a = 1; return a + 1; }")
        assert js == "const a = 1; return a + 1;"

    def test_fleche_expression(self):
        js = playwright_js_to_iife("() => document.title")
        assert js == "return document.title;"

    def test_multiligne_avec_newlines(self):
        js = playwright_js_to_iife("() => {\n  const a = 1;\n  return a;\n}")
        assert "const a = 1;" in js
        assert "return a;" in js

    def test_script_brut_inchange(self):
        raw = "return navigator.webdriver;"
        assert playwright_js_to_iife(raw) == raw

    def test_fleche_avec_parametre(self):
        js = playwright_js_to_iife("(el) => el.scrollHeight")
        assert js == "return el.scrollHeight;"
