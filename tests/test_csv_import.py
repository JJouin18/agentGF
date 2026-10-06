import pytest

from agentgf import csv_import
from agentgf.db import connect
from agentgf.tools import BudgetTools, ToolError


def parse(text):
    return csv_import.parse(text)


@pytest.mark.parametrize("raw, expected", [
    ("45,90", 45.90), ("-45,90", -45.90), ("1 234,56", 1234.56), ("1 234,56 €", 1234.56),
    ("-1.234,56", -1234.56), ("1,234.56", 1234.56), ("+12,00", 12.0), ("12.5", 12.5), ("45,90-", -45.90),
    ("1.234.567", 1234567.0),
])
def test_parse_amount(raw, expected):
    assert csv_import.parse_amount(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "12,3,4"])
def test_parse_amount_invalid(raw):
    with pytest.raises(ValueError):
        csv_import.parse_amount(raw)


@pytest.mark.parametrize("raw", ["2026-10-01", "01/10/2026", "01/10/26", "01-10-2026", "01.10.2026",
                                 "2026-10-01 10:12:33"])
def test_parse_date(raw):
    assert csv_import.parse_date(raw) == "2026-10-01"


def test_debit_credit_with_bank_preamble_and_cp1252():
    # Export type Crédit Agricole : lignes d'en-tête de la banque, colonnes Débit/Crédit, Windows-1252.
    text = ("Téléchargement du 06/10/2026\n"
            "Compte de Dépôt carte n° 0123\n"
            "Solde au 06/10/2026 1 234,56 €\n"
            "\n"
            "Date;Libellé;Débit euros;Crédit euros;\n"
            "05/10/2026;\"CARTE X1234 04/10 CARREFOUR\n PARIS\";45,90;;\n"
            "03/10/2026;VIREMENT EN VOTRE FAVEUR SALAIRE;;2 450,00;\n"
            ";Total;45,90;2 450,00;\n")
    parsed = parse(csv_import.decode(text.encode("cp1252")))
    assert parsed.columns["date"] == "Date" and parsed.columns["debit"] == "Débit euros"
    assert [(r.date, r.amount) for r in parsed.rows] == [("2026-10-05", -45.90), ("2026-10-03", 2450.0)]
    assert parsed.rows[0].description == "CARTE X1234 04/10 CARREFOUR PARIS"
    assert parsed.errors == []


def test_boursorama_style():
    text = ("dateOp;dateVal;label;category;categoryParent;supplierFound;amount;accountNum\n"
            "2026-10-02;2026-10-02;\"CARTE 01/10/26 NETFLIX\";Abonnements;Loisirs;netflix;-13,49;0001\n")
    row = parse(text).rows[0]
    assert (row.date, row.amount, row.category) == ("2026-10-02", -13.49, "abonnements")


def test_revolut_style():
    text = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
            "CARD_PAYMENT,Current,2026-10-01 10:12:33,2026-10-02 08:00:00,Uber Eats,-12.50,0.00,EUR,COMPLETED,100.00\n"
            "TOPUP,Current,2026-10-01 09:00:00,2026-10-01 09:00:01,Top-up,200.00,0.00,EUR,COMPLETED,112.50\n")
    parsed = parse(text)
    assert parsed.columns["date"] == "Started Date"
    assert [(r.date, r.amount, r.description) for r in parsed.rows] == [
        ("2026-10-01", -12.5, "Uber Eats"), ("2026-10-01", 200.0, "Top-up")]


def test_bnp_and_societe_generale_style():
    bnp = ("Date opération;Libellé court;Type opération;Libellé opération;Montant opération\n"
           "04/10/2026;CB LIDL;CARTE;FACTURE CARTE DU 03/10 LIDL 1234;-32,10\n")
    row = parse(bnp).rows[0]
    assert (row.amount, row.description) == (-32.10, "FACTURE CARTE DU 03/10 LIDL 1234")
    sg = ("=\"0001234567\";01/10/2026;06/10/2026;\n"
          "Date de l'opération;Libellé;Détail de l'écriture;Montant de l'opération;Devise\n"
          "02/10/2026;PRLV SEPA EDF;EDF clients particuliers;-89,00;EUR\n")
    assert parse(sg).rows[0].amount == -89.0


def test_errors_and_missing_columns():
    parsed = parse("date;montant;libelle\n2026-10-01;abc;X\n32/13/2026;-5;Y\n2026-10-02;-5;Z\n")
    assert len(parsed.rows) == 1 and len(parsed.errors) == 2
    with pytest.raises(csv_import.CSVFormatError):
        parse("nom;prenom\nDupont;Jean\n")
    with pytest.raises(csv_import.CSVFormatError):
        parse("\n\n")


def test_import_dedupes_and_dry_run(tmp_path):
    tools = BudgetTools(connect(":memory:"))
    f = tmp_path / "releve.csv"
    f.write_text("Date;Libellé;Montant\n01/10/2026;CAFE;-2,00\n01/10/2026;CAFE;-2,00\n02/10/2026;CB LIDL;-30,00\n",
                 encoding="utf-8")
    preview = tools.import_csv(str(f), dry_run=True)
    assert (preview["imported"], preview["duplicates"]) == (3, 0)
    assert tools.list_transactions()["count"] == 0
    assert preview["period"] == {"start": "2026-10-01", "end": "2026-10-02"}
    assert preview["sample"][2]["category"] == "alimentation"

    assert tools.import_csv(str(f))["imported"] == 3
    # Relevé suivant qui chevauche le précédent : seules les nouvelles lignes sont ajoutées.
    f.write_text("Date;Libellé;Montant\n01/10/2026;CAFE;-2,00\n01/10/2026;CAFE;-2,00\n01/10/2026;CAFE;-2,00\n"
                 "02/10/2026;CB LIDL;-30,00\n05/10/2026;SALAIRE;2000\n", encoding="utf-8")
    r = tools.import_csv(str(f))
    assert (r["imported"], r["duplicates"]) == (2, 3)
    assert tools.list_transactions()["count"] == 5
    # Les doublons sont comptés par compte.
    tools.create_account("Livret", "livret")
    assert tools.import_csv(str(f), account="Livret", dry_run=True)["duplicates"] == 0
    with pytest.raises(ToolError):
        f.write_text("a;b\n1;2\n", encoding="utf-8")
        tools.import_csv(str(f))
