"""Lecture des relevés CSV exportés par les banques.

Gère les variantes courantes des exports français et européens : encodage UTF-8 ou Windows-1252,
séparateur ; , ou tabulation, lignes d'en-tête de la banque avant le tableau, dates JJ/MM/AAAA ou ISO,
montant signé ou colonnes Débit / Crédit séparées, montants « 1 234,56 € » ou « -1,234.56 ».
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

from .categorize import normalize

# Alias de colonnes (normalisés : minuscules, sans accents). Le premier trouvé l'emporte.
DATE_ALIASES = ["date operation", "date de l operation", "date", "date comptable", "date de comptabilisation",
                "started date", "booking date", "transaction date", "date valeur", "date de valeur",
                "completed date", "value date"]
AMOUNT_ALIASES = ["montant", "montant eur", "montant euros", "montant en euros", "amount", "somme"]
DEBIT_ALIASES = ["debit", "debit eur", "debit euros", "debits", "montant debit", "sortie"]
CREDIT_ALIASES = ["credit", "credit eur", "credit euros", "credits", "montant credit", "entree"]
LABEL_ALIASES = ["libelle", "libelle operation", "libelle de l operation", "libelle simplifie", "description",
                 "label", "detail", "details", "intitule", "nature de l operation", "operation",
                 "beneficiaire", "payee", "reference", "merchant"]
CATEGORY_ALIASES = ["categorie", "category", "sous categorie", "categorie operation"]

DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M"]


class CSVFormatError(ValueError):
    pass


@dataclass
class ParsedRow:
    line: int
    date: str
    amount: float            # signé : négatif = dépense
    description: str
    category: str | None


@dataclass
class ParsedCSV:
    columns: dict[str, str | None]
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_date(value: str) -> str:
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"date illisible : '{value}'")


def parse_amount(value: str) -> float:
    s = value.strip().replace(" ", "").replace(" ", "").replace(" ", "")
    s = re.sub(r"(EUR|€)", "", s, flags=re.IGNORECASE)
    negative = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("+-()")
    if not s:
        raise ValueError("montant vide")
    if "," in s and "." in s:
        # Le dernier séparateur est le séparateur décimal : « 1.234,56 » ou « 1,234.56 ».
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        raise ValueError(f"montant illisible : '{value}'")
    return -float(s) if negative else float(s)


def _find(headers: list[str], aliases: list[str]) -> int | None:
    for alias in aliases:
        for i, h in enumerate(headers):
            if h == alias:
                return i
    for alias in aliases:  # repli : l'en-tête commence par l'alias (« Montant (EUR) », « Date opé. »)
        for i, h in enumerate(headers):
            if h.startswith(alias):
                return i
    return None


def _sniff_delimiter(lines: list[str]) -> str:
    sample = [line for line in lines[:40] if line.strip()]
    counts = {d: sum(line.count(d) for line in sample) for d in (";", "\t", ",")}
    return max(counts, key=counts.get)


def parse(text: str) -> ParsedCSV:
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        raise CSVFormatError("le fichier est vide")
    delimiter = _sniff_delimiter(lines)
    table = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))

    # La ligne d'en-tête est la première qui contient une colonne de date et une colonne de montant.
    for header_index, row in enumerate(table[:40]):
        headers = [normalize(c) for c in row]
        idx_date = _find(headers, DATE_ALIASES)
        idx_amount = _find(headers, AMOUNT_ALIASES)
        idx_debit, idx_credit = _find(headers, DEBIT_ALIASES), _find(headers, CREDIT_ALIASES)
        if idx_date is not None and (idx_amount is not None or idx_debit is not None or idx_credit is not None):
            break
    else:
        raise CSVFormatError(
            "colonnes introuvables : le fichier doit contenir une colonne de date et une colonne "
            "« Montant » (ou « Débit » / « Crédit »)")

    idx_label = _find(headers, LABEL_ALIASES)
    idx_category = _find(headers, CATEGORY_ALIASES)
    original = table[header_index]
    name = lambda i: original[i].strip() if i is not None else None  # noqa: E731
    result = ParsedCSV(columns={
        "date": name(idx_date),
        "montant": name(idx_amount),
        "debit": name(idx_debit) if idx_amount is None else None,
        "credit": name(idx_credit) if idx_amount is None else None,
        "libelle": name(idx_label),
        "categorie": name(idx_category),
    })

    for offset, row in enumerate(table[header_index + 1:], start=header_index + 2):
        if not any(c.strip() for c in row):
            continue
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""  # noqa: E731
        raw_date = cell(idx_date)
        if not raw_date:
            continue  # ligne de total ou de pied de page
        try:
            d = parse_date(raw_date)
            if idx_amount is not None:
                amount = parse_amount(cell(idx_amount))
            else:
                debit, credit = cell(idx_debit), cell(idx_credit)
                amount = (parse_amount(credit) if credit else 0) - (abs(parse_amount(debit)) if debit else 0)
        except ValueError as e:
            result.errors.append(f"ligne {offset} : {e}")
            continue
        if amount == 0:
            continue
        description = re.sub(r"\s+", " ", cell(idx_label))
        category = cell(idx_category).lower() or None
        result.rows.append(ParsedRow(offset, d, round(amount, 2), description, category))
    return result
