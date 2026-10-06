"""Catégorisation automatique des transactions à partir de leur libellé.

Ordre de priorité :
1. règles de l'utilisateur (table category_rules), le motif le plus long l'emporte ;
2. historique : catégorie la plus fréquente pour un libellé identique déjà classé ;
3. mots-clés intégrés (enseignes et libellés bancaires courants en France).
Sinon la transaction reste « a_categoriser » et Claude peut la classer lui-même.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata

from .db import TRANSFER_CATEGORY, UNCATEGORIZED

BUILTIN_RULES: dict[str, str] = {
    # Alimentation
    "carrefour": "alimentation", "leclerc": "alimentation", "auchan": "alimentation",
    "lidl": "alimentation", "aldi": "alimentation", "intermarche": "alimentation",
    "monoprix": "alimentation", "franprix": "alimentation", "casino": "alimentation",
    "super u": "alimentation", "picard": "alimentation", "biocoop": "alimentation",
    "boulangerie": "alimentation", "grand frais": "alimentation",
    # Restaurants
    "restaurant": "restaurants", "mcdonald": "restaurants", "burger king": "restaurants",
    "kfc": "restaurants", "deliveroo": "restaurants", "uber eats": "restaurants",
    "just eat": "restaurants", "starbucks": "restaurants",
    # Transport
    "sncf": "transport", "ratp": "transport", "navigo": "transport", "uber": "transport",
    "blablacar": "transport", "bolt": "transport", "totalenergies": "transport",
    "station service": "transport", "esso": "transport", "peage": "transport",
    "vinci autoroutes": "transport", "parking": "transport",
    # Logement & énergie
    "loyer": "logement", "edf": "logement", "engie": "logement", "veolia": "logement",
    "assurance habitation": "logement", "taxe fonciere": "logement",
    # Télécom & abonnements
    "free mobile": "telecom", "freebox": "telecom", "orange": "telecom", "sfr": "telecom",
    "bouygues": "telecom", "sosh": "telecom", "netflix": "abonnements",
    "spotify": "abonnements", "deezer": "abonnements", "disney": "abonnements",
    "canal+": "abonnements", "amazon prime": "abonnements", "youtube premium": "abonnements",
    "salle de sport": "abonnements", "basic fit": "abonnements",
    # Santé
    "pharmacie": "sante", "doctolib": "sante", "mutuelle": "sante", "medecin": "sante",
    "dentiste": "sante", "opticien": "sante",
    # Achats
    "amazon": "achats", "fnac": "achats", "darty": "achats", "decathlon": "achats",
    "ikea": "achats", "zara": "achats", "leroy merlin": "achats", "cdiscount": "achats",
    # Loisirs
    "cinema": "loisirs", "ugc": "loisirs", "pathe": "loisirs", "steam": "loisirs",
    "playstation": "loisirs", "airbnb": "voyages", "booking": "voyages", "air france": "voyages",
    # Revenus
    "salaire": "salaire", "paie": "salaire", "caf": "aides", "pole emploi": "aides",
    "france travail": "aides", "remboursement": "remboursements",
    # Divers
    "retrait dab": "especes", "retrait": "especes", "impots": "impots", "dgfip": "impots",
    "virement interne": TRANSFER_CATEGORY,
}


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation superflue, espaces compactés."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9+ ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _match(description: str, rules: dict[str, str]) -> str | None:
    padded = f" {description} "
    for pattern in sorted(rules, key=len, reverse=True):
        if f" {pattern} " in padded or (len(pattern) >= 7 and pattern in description):
            return rules[pattern]
    return None


def categorize(conn: sqlite3.Connection, description: str) -> str:
    desc = normalize(description)
    if not desc:
        return UNCATEGORIZED

    user_rules = {normalize(r["pattern"]): r["category"] for r in conn.execute("SELECT * FROM category_rules")}
    if found := _match(desc, user_rules):
        return found

    row = conn.execute(
        "SELECT category, COUNT(*) AS n FROM transactions WHERE lower(description) = lower(?) "
        "AND category != ? GROUP BY category ORDER BY n DESC LIMIT 1",
        (description.strip(), UNCATEGORIZED),
    ).fetchone()
    if row:
        return row["category"]

    return _match(desc, BUILTIN_RULES) or UNCATEGORIZED
