"""Nettoyage de texte specifique aux plateformes.

Utilise par les parseurs (et par la regeneration d'exports) pour retirer les
artefacts qui ne font pas partie du message : balises internes Grok, marqueurs
de rendu, etc.
"""

from __future__ import annotations

import re

# Grok (xAI) : cartes de citation et balises internes injectees dans le texte
_GROK_RENDER = re.compile(r"<grok:render\b[^>]*>.*?</grok:render>", re.DOTALL | re.IGNORECASE)
_GROK_ARGUMENT = re.compile(r"<argument\b[^>]*>.*?</argument>", re.DOTALL | re.IGNORECASE)
_GROK_XAI = re.compile(r"</?xaiArtifact[^>]*>", re.IGNORECASE)
_GROK_STRAY = re.compile(r"</?(?:xai[A-Za-z]*|grok:[A-Za-z]+|argument)\b[^>]*>", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SPACES_BEFORE_NL = re.compile(r"[ \t]+\n")
_MULTI_NL = re.compile(r"\n{3,}")


def clean_grok_text(text: str) -> str:
    """Retire les cartes/balises Grok et normalise les espaces."""
    if not text:
        return text
    text = _GROK_RENDER.sub("", text)
    text = _GROK_ARGUMENT.sub("", text)
    text = _GROK_XAI.sub("", text)
    text = _BR.sub("\n", text)
    text = _GROK_STRAY.sub("", text)
    text = _SPACES_BEFORE_NL.sub("\n", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


#: nettoyage par plateforme (etendu au besoin)
CLEANERS = {"grok": clean_grok_text}


def clean_text(text: str, platform: str) -> str:
    cleaner = CLEANERS.get(platform)
    return cleaner(text) if cleaner else text
