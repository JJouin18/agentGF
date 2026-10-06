"""Outils métier exposés à Claude : transactions, budgets, épargne, projections."""

from __future__ import annotations

import calendar
import csv
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable


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


class BudgetTools:
    def __init__(self, conn: sqlite3.Connection, today: Callable[[], date] = date.today):
        self.conn = conn
        self.today = today

    # --- Transactions -------------------------------------------------------

    def add_transaction(self, amount: float, kind: str, category: str,
                        description: str = "", date: str | None = None) -> dict:
        if amount <= 0:
            raise ToolError("le montant doit être strictement positif")
        if kind not in ("revenu", "depense"):
            raise ToolError("kind doit valoir 'revenu' ou 'depense'")
        d = _parse_date(date) if date else self.today()
        category = category.strip().lower()
        cur = self.conn.execute(
            "INSERT INTO transactions (date, amount, kind, category, description) VALUES (?, ?, ?, ?, ?)",
            (d.isoformat(), amount, kind, category, description),
        )
        self.conn.commit()
        result: dict[str, Any] = {"id": cur.lastrowid, "date": d.isoformat(), "amount": _r(amount),
                                  "kind": kind, "category": category}
        if kind == "depense":
            status = self._category_status(category, d.strftime("%Y-%m"))
            if status:
                result["budget"] = status
        return result

    def list_transactions(self, start: str | None = None, end: str | None = None,
                          category: str | None = None, kind: str | None = None,
                          limit: int = 50) -> dict:
        query, params = "SELECT * FROM transactions WHERE 1=1", []
        if start:
            query += " AND date >= ?"
            params.append(_parse_date(start, "start").isoformat())
        if end:
            query += " AND date <= ?"
            params.append(_parse_date(end, "end").isoformat())
        if category:
            query += " AND category = ?"
            params.append(category.strip().lower())
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY date DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = [dict(r) for r in self.conn.execute(query, params)]
        return {"count": len(rows), "transactions": rows}

    def delete_transaction(self, transaction_id: int) -> dict:
        cur = self.conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
        self.conn.commit()
        if cur.rowcount == 0:
            raise ToolError(f"aucune transaction avec l'id {transaction_id}")
        return {"deleted": transaction_id}

    def import_csv(self, path: str) -> dict:
        """CSV avec colonnes : date, montant, categorie, description (montant négatif = dépense)."""
        p = Path(path).expanduser()
        if not p.is_file():
            raise ToolError(f"fichier introuvable : {p}")
        imported, errors = 0, []
        with p.open(newline="", encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            for i, row in enumerate(csv.DictReader(f, dialect=dialect), start=2):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                try:
                    raw = row.get("montant") or row.get("amount") or ""
                    value = float(raw.replace(" ", "").replace(" ", "").replace(",", "."))
                    d = _parse_date(row.get("date", "")).isoformat()
                except (ValueError, ToolError) as e:
                    errors.append(f"ligne {i} : {e}")
                    continue
                if value == 0:
                    continue
                self.conn.execute(
                    "INSERT INTO transactions (date, amount, kind, category, description) VALUES (?, ?, ?, ?, ?)",
                    (d, abs(value), "revenu" if value > 0 else "depense",
                     (row.get("categorie") or row.get("category") or "divers").lower(),
                     row.get("description", "")),
                )
                imported += 1
        self.conn.commit()
        return {"imported": imported, "errors": errors[:20]}

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

    def monthly_summary(self, month: str | None = None) -> dict:
        month, first, last = _month_bounds(month)
        rows = self.conn.execute(
            "SELECT kind, category, SUM(amount) AS total, COUNT(*) AS n FROM transactions "
            "WHERE date BETWEEN ? AND ? GROUP BY kind, category ORDER BY total DESC",
            (first, last),
        ).fetchall()
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

    def spending_trends(self, months: int = 6) -> dict:
        """Totaux mensuels de revenus/dépenses sur les N derniers mois (mois courant inclus)."""
        months = max(1, min(months, 36))
        t = self.today()
        series = []
        for back in range(months - 1, -1, -1):
            y, m = divmod(t.year * 12 + t.month - 1 - back, 12)
            s = self.monthly_summary(f"{y:04d}-{m + 1:02d}")
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

TOOL_SCHEMAS = [
    _tool("add_transaction",
          "Enregistre un revenu ou une dépense. Renvoie aussi l'état du budget de la catégorie si un plafond existe.",
          {"amount": {"type": "number", "description": "Montant positif en euros"},
           "kind": {"type": "string", "enum": ["revenu", "depense"]},
           "category": _CATEGORY,
           "description": {"type": "string"},
           "date": {**_DATE, "description": "Date AAAA-MM-JJ (défaut : aujourd'hui)"}},
          ["amount", "kind", "category"]),
    _tool("list_transactions", "Liste les transactions, filtrables par période, catégorie et type.",
          {"start": _DATE, "end": _DATE, "category": _CATEGORY,
           "kind": {"type": "string", "enum": ["revenu", "depense"]},
           "limit": {"type": "integer", "description": "Nombre max de résultats (défaut 50)"}},
          []),
    _tool("delete_transaction", "Supprime une transaction par son id.",
          {"transaction_id": {"type": "integer"}}, ["transaction_id"]),
    _tool("import_csv",
          "Importe un relevé CSV local (colonnes date, montant, categorie, description ; montant négatif = dépense).",
          {"path": {"type": "string", "description": "Chemin du fichier CSV"}}, ["path"]),
    _tool("set_budget", "Crée ou met à jour le plafond mensuel d'une catégorie de dépenses.",
          {"category": _CATEGORY, "monthly_limit": {"type": "number", "description": "Plafond mensuel en euros"}},
          ["category", "monthly_limit"]),
    _tool("get_budget_status", "État de chaque budget (dépensé, restant, % utilisé, dépassement) pour un mois.",
          {"month": _MONTH}, []),
    _tool("monthly_summary",
          "Bilan d'un mois : revenus, dépenses, solde, taux d'épargne et répartition par catégorie.",
          {"month": _MONTH}, []),
    _tool("spending_trends", "Évolution mensuelle des revenus et dépenses sur les N derniers mois, avec moyennes.",
          {"months": {"type": "integer", "description": "Nombre de mois (défaut 6, max 36)"}}, []),
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
