"""Drivers de conversation : envoyer des messages/fichiers dans un chatbot.

Complement de la lecture (services/parsers) : les drivers pilotent la saisie
d'un message, l'upload de pieces jointes et l'attente de la reponse, via la
facade navigateur (`session`) commune a Playwright et Botasaurus.
"""

from .base import ChatDriver
from .registry import DRIVERS, get_driver

__all__ = ["ChatDriver", "DRIVERS", "get_driver"]
