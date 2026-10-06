import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agentgf.db import connect
from agentgf.tools import BudgetTools
from agentgf.web import App, make_handler


class FakeAgent:
    def __init__(self, tools):
        self.tools = tools

    def ask(self, message, on_tool=None):
        on_tool("add_transaction", {})
        self.tools.add_transaction(12, "depense", description="CB LIDL")
        return f"reçu : {message}"


@pytest.fixture
def server():
    tools = BudgetTools(connect(":memory:"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(tools, lambda: FakeAgent(tools))))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def call(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_index_and_dashboard(server):
    status, body = call(server + "/")
    assert status == 200 and b"AgentGF" in body
    status, body = call(server + "/api/dashboard?month=2026-10")
    d = json.loads(body)
    assert status == 200 and d["summary"]["month"] == "2026-10"
    assert d["accounts"]["accounts"][0]["name"] == "Compte courant"
    assert call(server + "/api/dashboard?month=bad")[0] == 400


def test_chat_and_import(server):
    status, body = call(server + "/api/chat", json.dumps({"message": "salut"}).encode(),
                        {"Content-Type": "application/json"})
    assert status == 200 and json.loads(body) == {"reply": "reçu : salut", "tools": ["add_transaction"]}
    csv = "date;montant;description\n2026-10-01;-20,5;CB AUCHAN\n2026-10-02;-7;XYZ\n".encode()
    status, body = call(server + "/api/import", csv, {"Content-Type": "text/csv"})
    assert status == 200
    assert json.loads(body) == {"imported": 2, "auto_categorized": 1, "uncategorized": 1, "errors": []}
    assert json.loads(call(server + "/api/dashboard")[1])["uncategorized"] == 1


def test_rejects_foreign_origin_and_host(server):
    status, _ = call(server + "/api/chat/reset", b"", {"Origin": "http://evil.example"})
    assert status == 403
    status, _ = call(server + "/api/dashboard", headers={"Host": "evil.example"})
    assert status == 403
