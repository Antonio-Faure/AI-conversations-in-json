"""Tests du nettoyage texte par plateforme (src/utils/cleanup.py)."""

from __future__ import annotations

from src.utils.cleanup import clean_grok_text, clean_text


def test_grok_render_et_argument_supprimes():
    text = (
        "Voici les chiffres.<grok:render card_id=\"a6fedb\" "
        "card_type=\"citation_card\"></grok:render> "
        "<argument name=\"citation_id\">5</argument> fin."
    )
    out = clean_grok_text(text)
    assert "grok:render" not in out
    assert "<argument" not in out
    assert "Voici les chiffres." in out and "fin." in out


def test_grok_xaiartifact_et_br():
    assert "xaiArtifact" not in clean_grok_text("a</xaiArtifact>b")
    assert clean_grok_text("ligne1<br>ligne2") == "ligne1\nligne2"


def test_grok_espaces_normalises():
    assert clean_grok_text("a\n\n\n\n b") == "a\n\n b"


def test_clean_text_plateforme_inconnue():
    assert clean_text("inchange", "inconnu") == "inchange"
