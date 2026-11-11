#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VEILLE PERSO
Surveille des sujets (presse, marchés, catastrophes naturelles) et t'envoie
une notification sur ton téléphone avec l'appli gratuite ntfy.

  Réglages : sujets.toml  (activer / désactiver / ajouter des sujets)
  Mémoire  : etat.json    (créé automatiquement, ne pas modifier)
  Secret   : variable NTFY_TOPIC (le nom de ton sujet ntfy)

Options :  --test   envoie juste une notification de test
           --essai  affiche les notifications au lieu de les envoyer
"""
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    sys.exit("Python 3.11 ou plus récent est nécessaire.")

DOSSIER = Path(__file__).resolve().parent
FICHIER_CONFIG = DOSSIER / "sujets.toml"
FICHIER_ETAT = DOSSIER / "etat.json"

SEUIL_PANNE = 6           # passages ratés d'affilée avant de te prévenir
CONSERVATION_JOURS = 30   # durée de conservation de la mémoire

# Éditions de Google Actualités : (langue, pays, ceid)
EDITIONS = {
    "fr": ("fr", "FR", "FR:fr"),
    "pt": ("pt-PT", "PT", "PT:pt-150"),
    "en": ("en-US", "US", "US:en"),
    "es": ("es", "ES", "ES:es"),
    "de": ("de", "DE", "DE:de"),
}
NIVEAUX_GDACS = {"green": 0, "orange": 1, "red": 2}
LIBELLES_GDACS = {"EQ": "Séisme", "TC": "Cyclone", "FL": "Inondation",
                  "VO": "Volcan", "DR": "Sécheresse", "WF": "Feu de forêt",
                  "TS": "Tsunami"}


def maintenant():
    return datetime.now(timezone.utc)


def log(texte):
    print(texte, flush=True)


# ───────────────────────── Outils réseau ─────────────────────────

def telecharger(url, tentatives=2, delai=25):
    erreur = None
    for i in range(tentatives):
