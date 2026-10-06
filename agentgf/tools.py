"""Outils métier exposés à Claude : comptes, transactions, budgets, épargne, projections."""

from __future__ import annotations

import calendar
import csv
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .categorize import categorize, normalize
from .db import DEFAULT_ACCOUNT_ID, TRANSFER_CATEGORY, UNCATEGORIZED

ACCOUNT_TYPES = ["courant", "livret", "epargne", "especes", "autre"]


class ToolError(Exception):
    """Erreur renvoyée à Claude comme tool_result en erreur (is_error=True)."""


def _parse_date(value: str, field: str = "date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} invalide : '{value}' (format attendu AAAA-MM-JJ)")


def _month_bounds(month: str | None) -> tuple[str, str, str]:
    """Renvoie (AAAA-MM, premier jour, dernier jour) pour le mois donné ou le mois courant."""
    if not month:
        month = date.today().strftime("%Y-%m")
    try:
        year, mon = (int(p) for p in month.split("-"))
        last = calendar.monthrange(year, mon)[1]
    except (ValueError, calendar.IllegalMonthError):
        raise ToolError(f"mois invalide : '{month}' (format attendu AAAA-MM)")
    return f"{year:04d}-{mon:02d}", f"{year:04d}-{mon:02d}-01", f"{year:04d}-{mon:02d}-{last:02d}"


def _months_until(deadline: date, today: date) -> int:
    months = (deadline.year - today.year) * 12 + (deadline.month - today.month)
    return max(months, 1)


def _r(x: float) -> float:
    return round(x + 0.0, 2)


def _parse_amount(raw: str) -> float:
    return float(raw.replace(" ", "").replace(" ", "").replace("€", "").replace(",", "."))


class BudgetTools:
    def __init__(self, conn: sqlite3.Connection, today: Callable[[], date] = date.today):
        self.conn = conn
        self.today = today

    # --- Comptes ------------------------------------------------------------

    def _account_id(self, account: int | str | None) -> int:
        """Résout un compte par id ou par nom (insensible à la casse). None = compte par défaut."""
        if account is None or account == "":
            return DEFAULT_ACCOUNT_ID
        if isinstance(account, int) or str(account).isdigit():
            row = self.conn.execute("SELECT id FROM accounts WHERE id = ?", (int(account),)).fetchone()
        else:
            row = self.conn.execute("SELECT id FROM accounts WHERE lower(name) = lower(?)",
                                    (str(account).strip(),)).fetchone()
        if row is None:
            names = [r[0] for r in self.conn.execute("SELECT name FROM accounts ORDER BY id")]
            raise ToolError(f"compte introuvable : '{account}'. Comptes existants : {', '.join(names)}")
        return row[0]

    def create_account(self, name: str, type: str = "courant", initial_balance: float = 0) -> dict:
        if type not in ACCOUNT_TYPES:
            raise ToolError(f"type invalide : choisir parmi {', '.join(ACCOUNT_TYPES)}")
        try:
            cur = self.conn.execute("INSERT INTO accounts (name, type, initial_balance) VALUES (?, ?, ?)",
                                    (name.strip(), type, initial_balance))
        except sqlite3.IntegrityError:
            raise ToolError(f"un compte nommé '{name}' existe déjà")
        self.conn.commit()
        return self._account_view(cur.lastrowid)

    def _account_view(self, account_id: int) -> dict:
        row = self.conn.execute(
            "SELECT a.*, "
            "COALESCE(SUM(CASE WHEN t.kind = 'revenu' THEN t.amount ELSE -t.amount END), 0) AS movements "
            "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id WHERE a.id = ? GROUP BY a.id",
            (account_id,),
        ).fetchone()
        return {"id": row["id"], "name": row["name"], "type": row["type"],
                "initial_balance": _r(row["initial_balance"]),
                "balance": _r(row["initial_balance"] + row["movements"])}

    def list_accounts(self) -> dict:
        accounts = [self._account_view(r[0]) for r in self.conn.execute("SELECT id FROM accounts ORDER BY id")]
        return {"accounts": accounts, "total_balance": _r(sum(a["balance"] for a in accounts))}

    def update_account(self, account: int | str, name: str | None = None, type: str | None = None,
                       initial_balance: float | None = None) -> dict:
        account_id = self._account_id(account)
        if type is not None and type not in ACCOUNT_TYPES:
            raise ToolError(f"type invalide : choisir parmi {', '.join(ACCOUNT_TYPES)}")
        for column, value in (("name", name), ("type", type), ("initial_balance", initial_balance)):
            if value is not None:
                try:
                    self.conn.execute(f"UPDATE accounts SET {column} = ? WHERE id = ?", (value, account_id))
                except sqlite3.IntegrityError:
                    raise ToolError(f"un compte nommé '{name}' existe déjà")
        self.conn.commit()
        return self._account_view(account_id)

    def delete_account(self, account: int | str) -> dict:
        account_id = self._account_id(account)
        if account_id == DEFAULT_ACCOUNT_ID:
            raise ToolError("le compte par défaut ne peut pas être supprimé")
        n = self.conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)).fetchone()[0]
        if n:
            raise ToolError(f"ce compte contient {n} transaction(s) : supprimez-les ou déplacez-les d'abord")
        self.conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self.conn.commit()
        return {"deleted": account_id}

    def transfer(self, from_account: int | str, to_account: int | str, amount: float,
                 date: str | None = None, description: str = "") -> dict:
        """Virement interne : n'est compté ni en revenu ni en dépense dans les bilans."""
        src, dst = self._account_id(from_account), self._account_id(to_account)
        if src == dst:
            raise ToolError("les comptes source et destination doivent être différents")
        if amount <= 0:
            raise ToolError("le montant doit être strictement positif")
        d = (_parse_date(date) if date else self.today()).isoformat()
        names = {r["id"]: r["name"] for r in self.conn.execute("SELECT id, name FROM accounts")}
        label = description or f"Virement {names[src]} → {names[dst]}"
        for kind, acc in (("depense", src), ("revenu", dst)):
            self.conn.execute(
                "INSERT INTO transactions (date, amount, kind, category, description, account_id) "
                "VALUES (?, ?, ?, ?, ?, ?)", (d, amount, kind, TRANSFER_CATEGORY, label, acc))
        self.conn.commit()
        return {"from": self._account_view(src), "to": self._account_view(dst), "amount": _r(amount), "date": d}

    # --- Transactions -------------------------------------------------------

    def add_transaction(self, amount: float, kind: str, category: str | None = None,
                        description: str = "", date: str | None = None,
                        account: int | str | None = None) -> dict:
        if amount <= 0:
            raise ToolError("le montant doit être strictement positif")
        if kind not in ("revenu", "depense"):
            raise ToolError("kind doit valoir 'revenu' ou 'depense'")
        d = _parse_date(date) if date else self.today()
        account_id = self._account_id(account)
        category = category.strip().lower() if category else categorize(self.conn, description)
        cur = self.conn.execute(
            "INSERT INTO transactions (date, amount, kind, category, description, account_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (d.isoformat(), amount, kind, category, description, account_id),
        )
        self.conn.commit()
        result: dict[str, Any] = {"id": cur.lastrowid, "date": d.isoformat(), "amount": _r(amount),
                                  "kind": kind, "category": category, "account_id": account_id}
        if kind == "depense":
            status = self._category_status(category, d.strftime("%Y-%m"))
            if status:
                result["budget"] = status
        return result

    def list_transactions(self, start: str | None = None, end: str | None = None,
                          category: str | None = None, kind: str | None = None,
                          account: int | str | None = None, search: str | None = None,
                          limit: int = 50) -> dict:
        query = ("SELECT t.*, a.name AS account FROM transactions t JOIN accounts a ON a.id = t.account_id "
                 "WHERE 1=1")
        params: list[Any] = []
        if start:
            query += " AND t.date >= ?"
            params.append(_parse_date(start, "start").isoformat())
        if end:
            query += " AND t.date <= ?"
            params.append(_parse_date(end, "end").isoformat())
        if category:
            query += " AND t.category = ?"
            params.append(category.strip().lower())
        if kind:
            query += " AND t.kind = ?"
            params.append(kind)
        if account not in (None, ""):
            query += " AND t.account_id = ?"
            params.append(self._account_id(account))
        if search:
            query += " AND lower(t.description) LIKE ?"
            params.append(f"%{search.lower()}%")
        query += " ORDER BY t.date DESC, t.id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = [dict(r) for r in self.conn.execute(query, params)]
        return {"count": len(rows), "transactions": rows}

    def delete_transaction(self, transaction_id: int) -> dict:
        cur = self.conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise ToolError(f"aucune transaction avec l'id {transaction_id}")
        return {"deleted": transaction_id}

    def import_csv(self, path: str, account: int | str | None = None) -> dict:
        """CSV : date, montant (négatif = dépense), categorie (optionnelle), description.

        Les lignes sans catégorie sont classées automatiquement (règles, historique, mots-clés).
        """
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"fichier introuvable : {p}")
        account_id = self._account_id(account)
        imported, auto, pending, errors = 0, 0, 0, []
        with p.open(newline="", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            for i, row in enumerate(csv.DictReader(f, dialect=dialect), start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                try:
                    value = _parse_amount(row.get("montant") or row.get("amount") or "")
                    d = _parse_date(row.get("date", "")).isoformat()
                except (ValueError, ToolError) as e:
                    errors.append(f"ligne {i} : {e}")
                    continue
                if value == 0:
                    continue
                description = row.get("description") or row.get("libelle") or ""
                category = (row.get("categorie") or row.get("category") or "").lower()
                if not category:
                    category = categorize(self.conn, description)
                    if category == UNCATEGORIZED:
                        pending += 1
                    else:
                        auto += 1
                self.conn.execute(
                    "INSERT INTO transactions (date, amount, kind, category, description, account_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (d, abs(value), "revenu" if value > 0 else "depense", category, description, account_id),
                )
                imported += 1
        self.conn.commit()
        return {"imported": imported, "auto_categorized": auto, "uncategorized": pending,
                "errors": errors[:20]}

    # --- Catégorisation -----------------------------------------------------

    def list_uncategorized(self, limit: int = 100) -> dict:
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, date, amount, kind, description FROM transactions WHERE category = ? "
            "ORDER BY date DESC LIMIT ?", (UNCATEGORIZED, max(1, min(limit, 500))))]
        total = self.conn.execute("SELECT COUNT(*) FROM transactions WHERE category = ?",
                                  (UNCATEGORIZED,)).fetchone()[0]
        categories = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT category FROM transactions WHERE category NOT IN (?, ?) ORDER BY category",
            (UNCATEGORIZED, TRANSFER_CATEGORY))]
        return {"total": total, "transactions": rows, "existing_categories": categories}

    def recategorize_transactions(self, updates: list[dict]) -> dict:
        done, missing = 0, []
        for u in updates:
            cur = self.conn.execute("UPDATE transactions SET category = ? WHERE id = ?",
                                    (u["category"].strip().lower(), u["transaction_id"]))
            if cur.rowcount:
                done += 1
            else:
                missing.append(u["transaction_id"])
        self.conn.commit()
        return {"updated": done, "not_found": missing}

    def add_category_rule(self, pattern: str, category: str, apply_to_existing: bool = True) -> dict:
        """Règle : tout libellé contenant `pattern` est classé dans `category` (prioritaire)."""
        pattern, category = normalize(pattern), category.strip().lower()
        if len(pattern) < 3:
            raise ToolError("le motif doit contenir au moins 3 caractères")
        self.conn.execute(
            "INSERT INTO category_rules (pattern, category) VALUES (?, ?) "
            "ON CONFLICT(pattern) DO UPDATE SET category = excluded.category", (pattern, category))
        updated = 0
        if apply_to_existing:
            rows = self.conn.execute("SELECT id, description FROM transactions WHERE category = ?",
                                     (UNCATEGORIZED,)).fetchall()
            ids = [r["id"] for r in rows if pattern in normalize(r["description"])]
            for tid in ids:
                self.conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (category, tid))
            updated = len(ids)
        self.conn.commit()
        return {"pattern": pattern, "category": category, "recategorized": updated}

    def list_category_rules(self) -> dict:
        return {"rules": [dict(r) for r in self.conn.execute("SELECT * FROM category_rules ORDER BY pattern")]}

    def delete_category_rule(self, pattern: str) -> dict:
        cur = self.conn.execute("DELETE FROM category_rules WHERE pattern = ?", (normalize(pattern),))
        self.conn.commit()
        if cur.rowcount == 0:
            raise ToolError(f"aucune règle pour le motif '{pattern}'")
        return {"deleted": normalize(pattern)}

    # --- Budgets ------------------------------------------------------------

    def set_budget(self, category: str, monthly_limit: float) -> dict:
        if monthly_limit < 0:
            raise ToolError("le plafond doit être positif ou nul")
        category = category.strip().lower()
        self.conn.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit",
            (category, monthly_limit),
        )
        self.conn.commit()
        return {"category": category, "monthly_limit": _r(monthly_limit)}

    def delete_budget(self, category: str) -> dict:
        cur = self.conn.execute("DELETE FROM budgets WHERE category = ?", (category.strip().lower(),))
        self.conn.commit()
        if cur.rowcount == 0:
            raise ToolError(f"aucun budget pour la catégorie '{category}'")
        return {"deleted": category.strip().lower()}

    def _spent(self, category: str, first: str, last: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE kind = 'depense' AND category = ? AND date BETWEEN ? AND ?",
            (category, first, last),
        ).fetchone()
        return row[0]

    def _category_status(self, category: str, month: str) -> dict | None:
        row = self.conn.execute("SELECT monthly_limit FROM budgets WHERE category = ?", (category,)).fetchone()
        if row is None:
            return None
        _, first, last = _month_bounds(month)
        limit, spent = row[0], self._spent(category, first, last)
        return {
            "category": category,
            "limit": _r(limit),
            "spent": _r(spent),
            "remaining": _r(limit - spent),
            "percent_used": _r(100 * spent / limit) if limit else None,
            "over_budget": spent > limit,
        }

    def get_budget_status(self, month: str | None = None) -> dict:
        month, _, _ = _month_bounds(month)
        categories = [r[0] for r in self.conn.execute("SELECT category FROM budgets ORDER BY category")]
        return {"month": month, "budgets": [self._category_status(c, month) for c in categories]}

    # --- Analyses -----------------------------------------------------------

    def monthly_summary(self, month: str | None = None, account: int | str | None = None) -> dict:
        """Les virements internes sont exclus : ils ne sont ni des revenus ni des dépenses."""
        month, first, last = _month_bounds(month)
        query = ("SELECT kind, category, SUM(amount) AS total, COUNT(*) AS n FROM transactions "
                 "WHERE date BETWEEN ? AND ? AND category != ?")
        params: list[Any] = [first, last, TRANSFER_CATEGORY]
        if account not in (None, ""):
            query += " AND account_id = ?"
            params.append(self._account_id(account))
        rows = self.conn.execute(query + " GROUP BY kind, category ORDER BY total DESC", params).fetchall()
        income = sum(r["total"] for r in rows if r["kind"] == "revenu")
        expenses = sum(r["total"] for r in rows if r["kind"] == "depense")
        return {
            "month": month,
            "income": _r(income),
            "expenses": _r(expenses),
            "net": _r(income - expenses),
            "savings_rate_percent": _r(100 * (income - expenses) / income) if income else None,
            "expenses_by_category": [
                {"category": r["category"], "total": _r(r["total"]), "count": r["n"],
                 "share_percent": _r(100 * r["total"] / expenses) if expenses else 0}
                for r in rows if r["kind"] == "depense"
            ],
            "income_by_category": [
                {"category": r["category"], "total": _r(r["total"])} for r in rows if r["kind"] == "revenu"
            ],
        }

    def spending_trends(self, months: int = 6, account: int | str | None = None) -> dict:
        """Totaux mensuels de revenus/dépenses sur les N derniers mois (mois courant inclus)."""
        months = max(1, min(months, 36))
        t = self.today()
        series = []
        for back in range(months - 1, -1, -1):
            y, m = divmod(t.year * 12 + t.month - 1 - back, 12)
            s = self.monthly_summary(f"{y:04d}-{m + 1:02d}", account)
            series.append({k: s[k] for k in ("month", "income", "expenses", "net")})
        active = [s for s in series if s["income"] or s["expenses"]]
        avg = lambda key: _r(sum(s[key] for s in active) / len(active)) if active else 0  # noqa: E731
        return {"series": series, "average_income": avg("income"),
                "average_expenses": avg("expenses"), "average_net": avg("net")}

    # --- Épargne ------------------------------------------------------------

    def create_savings_goal(self, name: str, target: float, deadline: str | None = None,
                            initial_amount: float = 0) -> dict:
        if target <= 0:
            raise ToolError("l'objectif doit être strictement positif")
        if deadline:
            deadline = _parse_date(deadline, "deadline").isoformat()
        try:
            cur = self.conn.execute(
                "INSERT INTO savings_goals (name, target, saved, deadline) VALUES (?, ?, ?, ?)",
                (name.strip(), target, max(initial_amount, 0), deadline),
            )
        except sqlite3.IntegrityError:
            raise ToolError(f"un objectif nommé '{name}' existe déjà")
        self.conn.commit()
        return self._goal_view(self.conn.execute("SELECT * FROM savings_goals WHERE id = ?",
                                                 (cur.lastrowid,)).fetchone())

    def contribute_to_goal(self, goal_id: int, amount: float) -> dict:
        """Versement (montant positif) ou retrait (montant négatif) sur un objectif."""
        row = self.conn.execute("SELECT * FROM savings_goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise ToolError(f"aucun objectif avec l'id {goal_id}")
        new_saved = row["saved"] + amount
        if new_saved < 0:
            raise ToolError(f"retrait impossible : seulement {_r(row['saved'])} € épargnés")
        self.conn.execute("UPDATE savings_goals SET saved = ? WHERE id = ?", (new_saved, goal_id))
        self.conn.commit()
        return self._goal_view(self.conn.execute("SELECT * FROM savings_goals WHERE id = ?",
                                                 (goal_id,)).fetchone())

    def delete_savings_goal(self, goal_id: int) -> dict:
        cur = self.conn.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise ToolError(f"aucun objectif avec l'id {goal_id}")
        return {"deleted": goal_id}

    def _goal_view(self, row: sqlite3.Row) -> dict:
        remaining = max(row["target"] - row["saved"], 0)
        view = {
            "id": row["id"], "name": row["name"], "target": _r(row["target"]),
            "saved": _r(row["saved"]), "remaining": _r(remaining),
            "progress_percent": _r(min(100 * row["saved"] / row["target"], 100)),
            "deadline": row["deadline"],
        }
        if row["deadline"] and remaining > 0:
            today = self.today()
            deadline = date.fromisoformat(row["deadline"])
            if deadline <= today:
                view["status"] = "échéance dépassée"
            else:
                n = _months_until(deadline, today)
                view["months_left"] = n
                view["monthly_needed"] = _r(remaining / n)
        elif remaining == 0:
            view["status"] = "atteint"
        return view

    def list_savings_goals(self) -> dict:
        goals = [self._goal_view(r) for r in self.conn.execute("SELECT * FROM savings_goals ORDER BY id")]
        return {
            "goals": goals,
            "total_saved": _r(sum(g["saved"] for g in goals)),
            "total_monthly_needed": _r(sum(g.get("monthly_needed", 0) for g in goals)),
        }

    def project_savings(self, monthly_contribution: float, years: float,
                        annual_rate_percent: float = 0, initial_amount: float = 0) -> dict:
        """Projection à intérêts composés mensuels avec versements en fin de mois."""
        if years <= 0 or years > 60:
            raise ToolError("years doit être compris entre 0 et 60")
        r = annual_rate_percent / 100 / 12
        months = round(years * 12)
        balance, yearly = initial_amount, []
        for m in range(1, months + 1):
            balance = balance * (1 + r) + monthly_contribution
            if m % 12 == 0 or m == months:
                yearly.append({"month": m, "balance": _r(balance)})
        contributed = initial_amount + monthly_contribution * months
        return {
            "final_balance": _r(balance),
            "total_contributed": _r(contributed),
            "interest_earned": _r(balance - contributed),
            "milestones": yearly,
        }

    # --- Dispatch -----------------------------------------------------------

    def run(self, name: str, args: dict) -> str:
        fn = getattr(self, name, None)
        if name not in TOOL_NAMES or fn is None:
            raise ToolError(f"outil inconnu : {name}")
        try:
            result = fn(**args)
        except TypeError as e:
            raise ToolError(f"arguments invalides pour {name} : {e}")
        return json.dumps(result, ensure_ascii=False)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_DATE = {"type": "string", "description": "Date au format AAAA-MM-JJ"}
_MONTH = {"type": "string", "description": "Mois au format AAAA-MM (défaut : mois courant)"}
_CATEGORY = {"type": "string", "description": "Catégorie en minuscules, ex. alimentation, logement, transport, loisirs, salaire"}
_ACCOUNT = {"type": "string", "description": "Nom ou id du compte (défaut : compte courant principal)"}

TOOL_SCHEMAS = [
    # Comptes
    _tool("create_account", "Crée un compte (courant, livret, épargne, espèces...) avec un solde de départ.",
          {"name": {"type": "string"}, "type": {"type": "string", "enum": ACCOUNT_TYPES},
           "initial_balance": {"type": "number", "description": "Solde à la date de création du compte"}},
          ["name"]),
    _tool("list_accounts", "Liste les comptes avec leur solde actuel et le solde total.", {}, []),
    _tool("update_account", "Renomme un compte, change son type ou corrige son solde de départ.",
          {"account": _ACCOUNT, "name": {"type": "string"},
           "type": {"type": "string", "enum": ACCOUNT_TYPES}, "initial_balance": {"type": "number"}},
          ["account"]),
    _tool("delete_account", "Supprime un compte vide (sans transactions).", {"account": _ACCOUNT}, ["account"]),
    _tool("transfer",
          "Virement interne entre deux comptes (ex. du compte courant vers le livret). "
          "Non compté comme revenu ni dépense.",
          {"from_account": _ACCOUNT, "to_account": _ACCOUNT, "amount": {"type": "number"},
           "date": _DATE, "description": {"type": "string"}},
          ["from_account", "to_account", "amount"]),
    # Transactions
    _tool("add_transaction",
          "Enregistre un revenu ou une dépense. Sans catégorie, elle est déduite du libellé. "
          "Renvoie l'état du budget de la catégorie si un plafond existe.",
          {"amount": {"type": "number", "description": "Montant positif en euros"},
           "kind": {"type": "string", "enum": ["revenu", "depense"]},
           "category": _CATEGORY,
           "description": {"type": "string", "description": "Libellé, ex. 'Carrefour Market'"},
           "date": {**_DATE, "description": "Date AAAA-MM-JJ (défaut : aujourd'hui)"},
           "account": _ACCOUNT},
          ["amount", "kind"]),
    _tool("list_transactions",
          "Liste les transactions, filtrables par période, catégorie, type, compte et texte du libellé.",
          {"start": _DATE, "end": _DATE, "category": _CATEGORY,
           "kind": {"type": "string", "enum": ["revenu", "depense"]}, "account": _ACCOUNT,
           "search": {"type": "string", "description": "Texte recherché dans le libellé"},
           "limit": {"type": "integer", "description": "Nombre max de résultats (défaut 50)"}},
          []),
    _tool("delete_transaction", "Supprime une transaction par son id.",
          {"transaction_id": {"type": "integer"}}, ["transaction_id"]),
    _tool("import_csv",
          "Importe un relevé CSV local (colonnes date, montant, description, categorie optionnelle ; "
          "montant négatif = dépense). Les lignes sans catégorie sont classées automatiquement.",
          {"path": {"type": "string", "description": "Chemin du fichier CSV"}, "account": _ACCOUNT},
          ["path"]),
    # Catégorisation
    _tool("list_uncategorized",
          "Liste les transactions restées 'a_categoriser' et les catégories déjà utilisées.",
          {"limit": {"type": "integer"}}, []),
    _tool("recategorize_transactions", "Change la catégorie de plusieurs transactions en une fois.",
          {"updates": {"type": "array", "items": {
              "type": "object",
              "properties": {"transaction_id": {"type": "integer"}, "category": _CATEGORY},
              "required": ["transaction_id", "category"], "additionalProperties": False}}},
          ["updates"]),
    _tool("add_category_rule",
          "Mémorise une règle : tout libellé contenant le motif ira dans la catégorie (imports futurs inclus).",
          {"pattern": {"type": "string", "description": "Mot ou enseigne, ex. 'boulangerie paul'"},
           "category": _CATEGORY,
           "apply_to_existing": {"type": "boolean",
                                 "description": "Reclasser aussi les transactions 'a_categoriser' (défaut : oui)"}},
          ["pattern", "category"]),
    _tool("list_category_rules", "Liste les règles de catégorisation de l'utilisateur.", {}, []),
    _tool("delete_category_rule", "Supprime une règle de catégorisation.",
          {"pattern": {"type": "string"}}, ["pattern"]),
    # Budgets
    _tool("set_budget", "Crée ou met à jour le plafond mensuel d'une catégorie de dépenses.",
          {"category": _CATEGORY, "monthly_limit": {"type": "number", "description": "Plafond mensuel en euros"}},
          ["category", "monthly_limit"]),
    _tool("delete_budget", "Supprime le plafond d'une catégorie.", {"category": _CATEGORY}, ["category"]),
    _tool("get_budget_status", "État de chaque budget (dépensé, restant, % utilisé, dépassement) pour un mois.",
          {"month": _MONTH}, []),
    # Analyses
    _tool("monthly_summary",
          "Bilan d'un mois : revenus, dépenses, solde, taux d'épargne et répartition par catégorie "
          "(tous comptes, ou un seul).",
          {"month": _MONTH, "account": _ACCOUNT}, []),
    _tool("spending_trends", "Évolution mensuelle des revenus et dépenses sur les N derniers mois, avec moyennes.",
          {"months": {"type": "integer", "description": "Nombre de mois (défaut 6, max 36)"}, "account": _ACCOUNT},
          []),
    # Épargne
    _tool("create_savings_goal",
          "Crée un objectif d'épargne. Avec une échéance, calcule l'effort mensuel nécessaire.",
          {"name": {"type": "string"}, "target": {"type": "number", "description": "Montant cible en euros"},
           "deadline": {**_DATE, "description": "Échéance AAAA-MM-JJ (optionnelle)"},
           "initial_amount": {"type": "number", "description": "Montant déjà épargné"}},
          ["name", "target"]),
    _tool("contribute_to_goal",
          "Ajoute un versement (montant positif) ou un retrait (montant négatif) à un objectif d'épargne.",
          {"goal_id": {"type": "integer"}, "amount": {"type": "number"}}, ["goal_id", "amount"]),
    _tool("delete_savings_goal", "Supprime un objectif d'épargne.",
          {"goal_id": {"type": "integer"}}, ["goal_id"]),
    _tool("list_savings_goals",
          "Liste les objectifs d'épargne avec progression et effort mensuel nécessaire.", {}, []),
    _tool("project_savings",
          "Simule la croissance d'une épargne à intérêts composés (versements mensuels, taux annuel).",
          {"monthly_contribution": {"type": "number"}, "years": {"type": "number"},
           "annual_rate_percent": {"type": "number", "description": "Taux annuel en %, ex. 3 pour 3 %"},
           "initial_amount": {"type": "number"}},
          ["monthly_contribution", "years"]),
]

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}
