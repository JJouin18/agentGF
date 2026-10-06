import json
from datetime import date

import pytest

from agentgf.db import connect
from agentgf.tools import TOOL_SCHEMAS, BudgetTools, ToolError

TODAY = date(2026, 10, 6)


@pytest.fixture
def tools():
    return BudgetTools(connect(":memory:"), today=lambda: TODAY)


def test_add_transaction_reports_budget(tools):
    tools.set_budget("Alimentation", 300)
    tools.add_transaction(250, "depense", "alimentation", "courses")
    r = tools.add_transaction(80, "depense", "alimentation")
    assert r["date"] == "2026-10-06"
    assert r["budget"]["spent"] == 330
    assert r["budget"]["remaining"] == -30
    assert r["budget"]["over_budget"] is True


def test_invalid_inputs(tools):
    with pytest.raises(ToolError):
        tools.add_transaction(-5, "depense", "x")
    with pytest.raises(ToolError):
        tools.add_transaction(5, "depense", "x", date="06/10/2026")
    with pytest.raises(ToolError):
        tools.monthly_summary("2026-13")
    with pytest.raises(ToolError):
        tools.run("drop_tables", {})


def test_monthly_summary_and_trends(tools):
    tools.add_transaction(2500, "revenu", "salaire", date="2026-10-01")
    tools.add_transaction(800, "depense", "logement", date="2026-10-02")
    tools.add_transaction(200, "depense", "loisirs", date="2026-10-03")
    tools.add_transaction(999, "depense", "loisirs", date="2026-09-30")
    s = tools.monthly_summary("2026-10")
    assert (s["income"], s["expenses"], s["net"]) == (2500, 1000, 1500)
    assert s["savings_rate_percent"] == 60
    assert s["expenses_by_category"][0] == {"category": "logement", "total": 800, "count": 1, "share_percent": 80}
    t = tools.spending_trends(3)
    assert [m["month"] for m in t["series"]] == ["2026-08", "2026-09", "2026-10"]
    assert t["average_expenses"] == pytest.approx((999 + 1000) / 2, abs=0.01)


def test_list_and_delete(tools):
    a = tools.add_transaction(10, "depense", "transport", date="2026-10-01")
    tools.add_transaction(20, "depense", "loisirs", date="2026-10-02")
    assert tools.list_transactions(category="transport")["count"] == 1
    tools.delete_transaction(a["id"])
    assert tools.list_transactions()["count"] == 1
    with pytest.raises(ToolError):
        tools.delete_transaction(a["id"])


def test_savings_goal_flow(tools):
    g = tools.create_savings_goal("Voyage", 3000, deadline="2027-06-01", initial_amount=600)
    assert g["months_left"] == 8
    assert g["monthly_needed"] == 300
    g = tools.contribute_to_goal(g["id"], 2400)
    assert g["status"] == "atteint" and g["progress_percent"] == 100
    with pytest.raises(ToolError):
        tools.contribute_to_goal(g["id"], -5000)
    with pytest.raises(ToolError):
        tools.create_savings_goal("Voyage", 100)
    assert tools.list_savings_goals()["total_saved"] == 3000


def test_project_savings(tools):
    flat = tools.project_savings(100, 2)
    assert flat["final_balance"] == 2400 and flat["interest_earned"] == 0
    r = tools.project_savings(100, 10, annual_rate_percent=3, initial_amount=1000)
    assert r["total_contributed"] == 13000
    assert 15300 < r["final_balance"] < 15500
    assert len(r["milestones"]) == 10


def test_import_csv(tools, tmp_path):
    f = tmp_path / "releve.csv"
    f.write_text("date;montant;categorie;description\n"
                 "2026-10-01;2 100,00;salaire;Paie\n"
                 "2026-10-02;-45,90;alimentation;Supermarché\n"
                 "bad;-1;x;y\n", encoding="utf-8")
    r = tools.import_csv(str(f))
    assert r["imported"] == 2 and len(r["errors"]) == 1
    assert tools.monthly_summary("2026-10")["net"] == pytest.approx(2054.10)


def test_run_returns_json_and_schemas_are_strict(tools):
    out = json.loads(tools.run("set_budget", {"category": "loisirs", "monthly_limit": 150}))
    assert out == {"category": "loisirs", "monthly_limit": 150}
    for schema in TOOL_SCHEMAS:
        assert schema["strict"] is True
        assert schema["input_schema"]["additionalProperties"] is False
        assert hasattr(tools, schema["name"])
