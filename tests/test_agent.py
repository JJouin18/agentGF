from types import SimpleNamespace as NS

import pytest

from agentgf.agent import BudgetAgent
from agentgf.db import connect
from agentgf.tools import BudgetTools


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def tool_use(id, name, input):
    return NS(type="tool_use", id=id, name=name, input=input)


def text(t):
    return NS(type="text", text=t)


def test_agent_runs_tools_then_answers():
    tools = BudgetTools(connect(":memory:"))
    client = FakeClient([
        NS(stop_reason="tool_use", content=[
            tool_use("t1", "add_transaction", {"amount": 54.2, "kind": "depense", "category": "alimentation"}),
            tool_use("t2", "delete_transaction", {"transaction_id": 999}),
        ]),
        NS(stop_reason="end_turn", content=[text("C'est noté !")]),
    ])
    agent = BudgetAgent(tools, client=client)
    assert agent.ask("J'ai payé 54,20 € de courses") == "C'est noté !"

    results = client.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True
    assert tools.list_transactions()["count"] == 1
    assert client.calls[0]["fallbacks"] == "default"


def test_history_rolled_back_on_api_error():
    client = FakeClient([
        NS(stop_reason="tool_use", content=[tool_use("t1", "list_savings_goals", {})]),
        ConnectionError("réseau coupé"),
    ])
    agent = BudgetAgent(BudgetTools(connect(":memory:")), client=client)
    with pytest.raises(ConnectionError):
        agent.ask("Où en sont mes objectifs ?")
    assert agent.messages == []


def test_refusal():
    client = FakeClient([NS(stop_reason="refusal", content=[])])
    agent = BudgetAgent(BudgetTools(connect(":memory:")), client=client)
    assert "ne peux pas" in agent.ask("...")
