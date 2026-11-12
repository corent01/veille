#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VEILLE PERSO
Surveille des sujets (presse, marchés, catastrophes naturelles) et t'envoie
une notification sur ton téléphone avec l'appli gratuite ntfy.

  Réglages : sujets.toml  (activer / désactiver / ajouter des sujets)
  Plaisirs : plaisirs.toml (blagues et anecdotes du jour, à compléter)
  Journal  : journal.md   (liste complète des articles, créé automatiquement)
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
FICHIER_JOURNAL = DOSSIER / "journal.md"
JOURNAL_JOURS = 3          # durée de conservation du journal

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
        try:
            requete = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (compatible; veille-perso/1.0)"})
            with urllib.request.urlopen(requete, timeout=delai) as reponse:
                return reponse.read()
        except Exception as e:  # réseau, erreur HTTP...
            erreur = e
            if i + 1 < tentatives:
                time.sleep(3)
    raise erreur


def notifier(topic, serveur, token, alerte):
    corps = {
        "topic": topic,
        "title": alerte["titre"][:200],
        "message": alerte["message"][:3000],
        "priority": alerte.get("priorite", 3),
        "tags": alerte.get("tags", []),
    }
    if alerte.get("lien"):
        corps["click"] = alerte["lien"]
    if alerte.get("bouton"):
        corps["actions"] = [{"action": "view", "label": alerte["bouton"][0],
                             "url": alerte["bouton"][1]}]
    entetes = {"Content-Type": "application/json", "User-Agent": "veille-perso/1.0"}
    if token:
        entetes["Authorization"] = "Bearer " + token
    donnees = json.dumps(corps).encode("utf-8")
    erreur = None
    for i in range(2):
        try:
            requete = urllib.request.Request(serveur.rstrip("/") + "/", data=donnees,
                                             headers=entetes, method="POST")
            with urllib.request.urlopen(requete, timeout=20) as reponse:
                reponse.read()
            return
        except Exception as e:
            erreur = e
            time.sleep(3)
    raise erreur


# ───────────────────────── Outils texte / mémoire ─────────────────────────

def norm(texte):
    """Minuscules sans accents, pour comparer des mots."""
    texte = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in texte if unicodedata.category(c) != "Mn")


def contient(titre, mots):
    t = norm(titre)
    return any(re.search(r"\b" + re.escape(norm(m)), t) for m in mots)


def deja_vu(vus, cle, jours):
    quand = vus.get(cle)
    return bool(quand) and maintenant() - datetime.fromisoformat(quand) < timedelta(days=jours)


def charger_etat():
    try:
        etat = json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        etat = {}
    etat.setdefault("vus", {})
    etat.setdefault("erreurs", {})
    return etat


def sauver_etat(etat):
    limite = maintenant() - timedelta(days=CONSERVATION_JOURS)
    for nom, cles in list(etat["vus"].items()):
        etat["vus"][nom] = {c: t for c, t in cles.items()
                            if datetime.fromisoformat(t) >= limite}
    etat["dernier_passage"] = maintenant().strftime("%Y-%m-%d")  # 1 commit/jour au moins
    FICHIER_ETAT.write_text(
        json.dumps(etat, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8")


def marquer(etat, alerte):
    vus = etat["vus"].setdefault(alerte["sujet"], {})
    ts = maintenant().isoformat(timespec="seconds")
    for cle in alerte.get("cles", []):
        vus[cle] = ts


# ───────────────────────── Source 1 : presse (Google Actualités) ─────────────────────────

def source_actus(s, vus):
    if "requete" not in s:
        raise ValueError("il manque la ligne 'requete'")
    edition = s.get("edition", "fr")
    if edition not in EDITIONS:
        raise ValueError(f"édition inconnue '{edition}' (choix : {', '.join(EDITIONS)})")
    langue, pays, ceid = EDITIONS[edition]
    requete = f'{s["requete"]} when:{s.get("fenetre", "1d")}'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": requete, "hl": langue, "gl": pays, "ceid": ceid})
    racine = ET.fromstring(telecharger(url))

    nouveaux = []
    for it in racine.iter("item"):
        titre = (it.findtext("title") or "").strip()
        lien = (it.findtext("link") or "").strip()
        source = (it.findtext("source") or "").strip()
        if source and titre.endswith(" - " + source):
            titre = titre[: -len(source) - 3].strip()
        if not titre or not lien:
            continue
        if s.get("mots_cles") and not contient(titre, s["mots_cles"]):
            continue
        if s.get("exclure") and contient(titre, s["exclure"]):
            continue
        identifiant = (it.findtext("guid") or lien).encode("utf-8")
        cle = "a:" + hashlib.sha1(identifiant).hexdigest()[:16]
        if deja_vu(vus, cle, CONSERVATION_JOURS):
            continue
        nouveaux.append({"cle": cle, "titre": titre, "source": source, "lien": lien})

    if len(nouveaux) < s.get("min_articles", 1):
        return []  # pas assez de recoupements : on attend le prochain passage
    gardes = nouveaux[: s.get("max_titres", 5)]
    lignes = [f"• {a['titre']}" + (f" ({a['source']})" if a["source"] else "") for a in gardes]
    if len(nouveaux) > len(gardes):
        lignes.append(f"… et {len(nouveaux) - len(gardes)} autre(s) : bouton « Tout voir »")
    return [{
        "titre": f"{s['nom']} : {len(nouveaux)} article(s)",
        "message": "\n".join(lignes),
        "priorite": s.get("priorite", 3),
        "tags": s.get("tags", ["newspaper"]),
        "lien": gardes[0]["lien"],
        "cles": [a["cle"] for a in nouveaux],
        "articles": nouveaux,
    }]


# ───────────────────────── Source 2 : marchés (FRED, base de la Réserve fédérale) ─────────────────────────

def lire_fred(serie, jours=60):
    debut = (maintenant() - timedelta(days=jours)).strftime("%Y-%m-%d")
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode(
        {"id": serie, "cosd": debut})
    texte = telecharger(url).decode("utf-8", "replace")
    if "," not in texte.split("\n", 1)[0]:
        raise ValueError("réponse inattendue de FRED")
    points = []
    for ligne in list(csv.reader(io.StringIO(texte)))[1:]:
        if len(ligne) >= 2:
            try:
                points.append((ligne[0], float(ligne[1])))
            except ValueError:
                pass  # valeur manquante (jour férié)
    return points


def declenche(valeur, seuil, sens):
    if sens == "hausse":
        return valeur >= seuil
    if sens == "baisse":
        return valeur <= -seuil
    return abs(valeur) >= seuil


def source_marche(s, vus):
    if "serie" not in s:
        raise ValueError("il manque la ligne 'serie'")
    points = lire_fred(s["serie"])
    if len(points) < 2:
        raise ValueError("pas assez de données reçues")
    date, valeur = points[-1]
    sens = s.get("sens", "les deux")
    rappel = s.get("rappel_jours", 3)
    raisons, cles = [], []

    precedent = points[-2][1]
    var_jour = (valeur / precedent - 1) * 100 if precedent else 0.0
    if "seuil_jour_pct" in s and declenche(var_jour, s["seuil_jour_pct"], sens):
        cle = f"jour:{date}"
        if not deja_vu(vus, cle, CONSERVATION_JOURS):
            raisons.append(f"{var_jour:+.1f} % en 1 jour")
            cles.append(cle)

    n = s.get("jours_tendance", 5)
    if "seuil_tendance_pct" in s and len(points) > n and points[-1 - n][1]:
        var_n = (valeur / points[-1 - n][1] - 1) * 100
        if declenche(var_n, s["seuil_tendance_pct"], sens) and not deja_vu(vus, "tendance", rappel):
            raisons.append(f"{var_n:+.1f} % sur {n} jours")
            cles.append("tendance")

    if "niveau_haut" in s and valeur >= s["niveau_haut"] and not deja_vu(vus, "niveau_haut", rappel):
        raisons.append(f"niveau élevé ({valeur:g} ≥ {s['niveau_haut']:g})")
        cles.append("niveau_haut")
    if "niveau_bas" in s and valeur <= s["niveau_bas"] and not deja_vu(vus, "niveau_bas", rappel):
        raisons.append(f"niveau bas ({valeur:g} ≤ {s['niveau_bas']:g})")
        cles.append("niveau_bas")

    if not raisons:
        return []
    icone = "chart_with_downwards_trend" if var_jour < 0 else "chart_with_upwards_trend"
    return [{
        "titre": f"{s['nom']} : {valeur:g}",
        "message": ", ".join(raisons) + f"\nDernière donnée : {date}",
        "priorite": s.get("priorite", 3),
        "tags": s.get("tags", [icone]),
        "lien": f"https://fred.stlouisfed.org/series/{s['serie']}",
        "cles": cles,
    }]


# ───────────────────────── Source 3 : catastrophes naturelles (GDACS, ONU/UE) ─────────────────────────

def source_catastrophes(s, vus):
    url = s.get("url", "https://www.gdacs.org/xml/rss.xml")
    racine = ET.fromstring(telecharger(url))
    ns = {"g": "http://www.gdacs.org"}
    niveau_min = s.get("niveau_min", "orange").lower()
    if niveau_min not in NIVEAUX_GDACS:
        raise ValueError("niveau_min doit être 'green', 'orange' ou 'red'")
    minimum = NIVEAUX_GDACS[niveau_min]
    types = [t.upper() for t in s.get("types", ["EQ", "TC", "FL", "VO", "TS", "WF"])]
    limite = maintenant() - timedelta(days=s.get("fenetre_jours", 3))

    trouves, cles, rouge = [], [], False
    for it in racine.iter("item"):
        niveau = (it.findtext("g:alertlevel", namespaces=ns) or "").strip().lower()
        genre = (it.findtext("g:eventtype", namespaces=ns) or "").strip().upper()
        titre = (it.findtext("title") or "").strip()
        lien = (it.findtext("link") or "").strip()
        if not titre or NIVEAUX_GDACS.get(niveau, -1) < minimum or genre not in types:
            continue
        if s.get("mots_cles") and not contient(titre, s["mots_cles"]):
            continue
        try:
            if parsedate_to_datetime(it.findtext("pubDate")) < limite:
                continue
        except Exception:
            pass  # date illisible : on garde l'alerte
        cle = f"gdacs:{lien or titre}:{niveau}"  # une hausse de niveau re-déclenche
        if deja_vu(vus, cle, CONSERVATION_JOURS):
            continue
        rouge = rouge or niveau == "red"
        trouves.append((genre, titre, lien))
        cles.append(cle)

    if not trouves:
        return []
    gardes = trouves[: s.get("max_titres", 5)]
    lignes = [f"• {LIBELLES_GDACS.get(g, g)} : {t}" for g, t, _ in gardes]
    if len(trouves) > len(gardes):
        lignes.append(f"… et {len(trouves) - len(gardes)} autre(s)")
    return [{
        "titre": f"{s['nom']} : {len(trouves)} alerte(s)",
        "message": "\n".join(lignes),
        "priorite": 5 if rouge else s.get("priorite", 4),
        "tags": ["rotating_light"] if rouge else s.get("tags", ["warning"]),
        "lien": gardes[0][2] or None,
        "cles": cles,
    }]


# ───────────────────────── Source 4 : blague et anecdote du jour ─────────────────────────

def source_du_jour(s, vus):
    maintenant_ = maintenant()
    if maintenant_.hour < s.get("heure_utc", 7):
        return []  # trop tôt : on attend l'heure choisie
    cle = "jour:" + maintenant_.strftime("%Y-%m-%d")
    if deja_vu(vus, cle, 2):
        return []  # déjà envoyé aujourd'hui
    with open(DOSSIER / s.get("fichier", "plaisirs.toml"), "rb") as f:
        contenu = tomllib.load(f)
    blagues, faits = contenu.get("blagues", []), contenu.get("faits", [])
    if not blagues and not faits:
        raise ValueError("plaisirs.toml ne contient ni blagues ni faits")
    n = maintenant_.toordinal()  # un nouveau numéro chaque jour
    lignes = []
    if blagues:
        lignes.append("😄 " + blagues[n % len(blagues)])
    if faits:
        lignes.append("🤔 Le saviez-vous ? " + faits[n % len(faits)])
    return [{
        "titre": "☀️ Ta dose de bonne humeur",
        "message": "\n\n".join(lignes),
        "priorite": s.get("priorite", 3),
        "tags": s.get("tags", ["sunny"]),
        "cles": [cle],
    }]


SOURCES = {"actus": source_actus, "marche": source_marche,
           "catastrophes": source_catastrophes, "du_jour": source_du_jour}


# ───────────────────────── Journal des articles ─────────────────────────

def ecrire_journal(entrees):
    """Ajoute en tête de journal.md les listes complètes d'articles (3 jours conservés)."""
    if not entrees:
        return
    ts = maintenant()
    nouveaux = []
    for nom, articles in entrees:
        lignes = [f"## {ts:%Y-%m-%d %H:%M} UTC · {nom} ({len(articles)})", ""]
        for a in articles:
            titre = a["titre"].replace("[", "(").replace("]", ")")
            source = f" ({a['source']})" if a["source"] else ""
            lignes.append(f"- [{titre}]({a['lien']}){source}")
        nouveaux.append("\n".join(lignes))
    try:
        ancien = FICHIER_JOURNAL.read_text(encoding="utf-8")
    except FileNotFoundError:
        ancien = ""
    limite = ts - timedelta(days=JOURNAL_JOURS)
    gardes = []
    for bloc in re.split(r"\n(?=## )", ancien):
        m = re.match(r"## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC", bloc)
        if m and datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc) >= limite:
            gardes.append(bloc.strip())
    entete = ("# Journal de la veille\n\nListes complètes des articles, du plus récent "
              "au plus ancien (3 derniers jours).\n")
    FICHIER_JOURNAL.write_text(entete + "\n" + "\n\n".join(nouveaux + gardes) + "\n",
                               encoding="utf-8")


# ───────────────────────── Programme principal ─────────────────────────

def main():
    args = sys.argv[1:]
    essai = "--essai" in args
    test = "--test" in args or os.environ.get("VEILLE_TEST", "").lower() == "true"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if not topic and not essai:
        sys.exit("Il manque NTFY_TOPIC (le nom de ton sujet ntfy). Voir LISEZMOI.md")

    with open(FICHIER_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    general = cfg.get("general", {})
    serveur = os.environ.get("NTFY_SERVER") or general.get("ntfy_serveur", "https://ntfy.sh")

    def envoyer(alerte):
        if essai:
            log(f"[ESSAI] (priorité {alerte.get('priorite', 3)}) {alerte['titre']}\n{alerte['message']}\n")
            return True
        try:
            notifier(topic, serveur, token, alerte)
            return True
        except Exception as e:
            log(f"✗ Envoi impossible ({alerte['titre']}) : {e}")
            return False

    if test:
        ok = envoyer({"titre": "✅ Test réussi",
                      "message": "Ta veille est bien reliée à ton téléphone.",
                      "priorite": 3, "tags": ["white_check_mark"]})
        sys.exit(0 if ok else 1)

    depot = os.environ.get("GITHUB_REPOSITORY", "")
    lien_journal = f"https://github.com/{depot}/blob/main/journal.md" if depot else None
    journal = []
    etat = charger_etat()
    sujets = [s for s in cfg.get("sujets", []) if s.get("actif", True)]
    alertes = []

    for s in sujets:
        nom = s.get("nom", "(sans nom)")
        vus = etat["vus"].setdefault(nom, {})
        try:
            genre = s.get("type", "actus")
            if genre not in SOURCES:
                raise ValueError(f"type inconnu '{genre}' (choix : {', '.join(SOURCES)})")
            trouvees = SOURCES[genre](s, vus)
            etat["erreurs"][nom] = 0
            log(f"✓ {nom} : {len(trouvees)} alerte(s)")
            for a in trouvees:
                a["sujet"] = nom
                if a.get("articles") and lien_journal:
                    a["bouton"] = ("Tout voir", lien_journal)
                alertes.append(a)
        except Exception as e:
            n = etat["erreurs"].get(nom, 0) + 1
            etat["erreurs"][nom] = n
            log(f"✗ {nom} : {e}")
            if n == SEUIL_PANNE:
                alertes.append({"sujet": nom, "cles": [], "priorite": 2, "tags": ["warning"],
                                "titre": f"⚠️ Source en panne : {nom}",
                                "message": f"{n} échecs d'affilée. Dernière erreur : {e}"})

    # Envoi : les plus importantes d'abord, le reste regroupé
    alertes.sort(key=lambda a: -a.get("priorite", 3))
    maximum = general.get("max_notifs_par_passage", 8)
    echecs = 0
    for a in alertes[:maximum]:
        if envoyer(a):
            marquer(etat, a)
            if a.get("articles"):
                journal.append((a["sujet"], a["articles"]))
        else:
            echecs += 1
    trop = alertes[maximum:]
    if trop:
        groupe = {"titre": f"{len(trop)} autre(s) alerte(s)",
                  "message": "\n".join(f"• {a['titre']}" for a in trop)[:3000],
                  "priorite": max(a.get("priorite", 3) for a in trop), "tags": ["package"]}
        if lien_journal and any(a.get("articles") for a in trop):
            groupe["bouton"] = ("Tout voir", lien_journal)
        if envoyer(groupe):
            for a in trop:
                marquer(etat, a)
                if a.get("articles"):
                    journal.append((a["sujet"], a["articles"]))
        else:
            echecs += 1

    # Message discret « tout va bien » de temps en temps
    jours = general.get("battement_jours", 7)
    if jours and not essai:
        dernier = etat.get("dernier_battement")
        if not dernier or maintenant() - datetime.fromisoformat(dernier) >= timedelta(days=jours):
            noms = ", ".join(s.get("nom", "?") for s in sujets)
            if envoyer({"titre": "👀 Veille active",
                        "message": f"{len(sujets)} sujet(s) surveillé(s) : {noms}",
                        "priorite": 1, "tags": ["eyes"]}):
                etat["dernier_battement"] = maintenant().isoformat(timespec="seconds")

    if not essai:
        ecrire_journal(journal)
        sauver_etat(etat)
    sys.exit(1 if echecs else 0)


if __name__ == "__main__":
    main()
