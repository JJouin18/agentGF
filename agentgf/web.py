"""Interface web locale : tableau de bord avec graphiques + discussion avec l'agent.

Serveur HTTP de la bibliothèque standard, sans dépendance supplémentaire. Il écoute par défaut
sur 127.0.0.1 uniquement : il n'a pas d'authentification et expose vos données financières.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic

from .agent import BudgetAgent
from .tools import BudgetTools, ToolError

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 5 * 1024 * 1024


class App:
    """État partagé du serveur. Un verrou sérialise l'accès à SQLite et à l'agent."""

    def __init__(self, tools: BudgetTools, agent_factory):
        self.tools = tools
        self.agent_factory = agent_factory
        self.agent: BudgetAgent | None = None
        self.lock = threading.Lock()

    def dashboard(self, month: str | None, account: str | None) -> dict:
        t = self.tools
        with self.lock:
            return {
                "summary": t.monthly_summary(month, account),
                "trends": t.spending_trends(6, account),
                "budgets": t.get_budget_status(month),
                "goals": t.list_savings_goals(),
                "accounts": t.list_accounts(),
                "uncategorized": t.list_uncategorized(limit=1)["total"],
                "recent": t.list_transactions(account=account, limit=10)["transactions"],
            }

    def chat(self, message: str) -> dict:
        with self.lock:
            if self.agent is None:
                self.agent = self.agent_factory()
            used: list[str] = []
            reply = self.agent.ask(message, on_tool=lambda name, _input: used.append(name))
            return {"reply": reply, "tools": used}

    def reset(self) -> dict:
        with self.lock:
            self.agent = None
        return {"ok": True}

    def import_csv(self, content: bytes, account: str | None, dry_run: bool = False) -> dict:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            with self.lock:
                return self.tools.import_csv(path, account or None, dry_run=dry_run)
        finally:
            Path(path).unlink(missing_ok=True)


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def make_handler(app: App, local_only: bool = True):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentGF"

        def _host_allowed(self) -> bool:
            # Protection contre le DNS rebinding : en mode local, seul un Host de bouclage est accepté.
            if not local_only:
                return True
            hostname = urlparse(f"//{self.headers.get('Host', '')}").hostname
            if hostname in LOOPBACK_HOSTS:
                return True
            self._error("hôte refusé", HTTPStatus.FORBIDDEN)
            return False

        def log_message(self, format, *args):  # noqa: A002 - silence des logs par requête
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: dict, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _error(self, message: str, status: int = 400) -> None:
            self._json({"error": message}, status)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ToolError("fichier trop volumineux (5 Mo max)")
            return self.rfile.read(length)

        def _internal_error(self, e: Exception) -> None:
            # Toujours une réponse JSON lisible, et le détail dans le terminal du serveur.
            traceback.print_exc(file=sys.stderr)
            self._error(f"erreur interne du serveur ({type(e).__name__} : {e}). "
                        "Le détail est affiché dans le terminal où tourne agentgf.", HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_GET(self):
            try:
                self._get()
            except Exception as e:  # noqa: BLE001
                self._internal_error(e)

        def do_POST(self):
            try:
                self._post()
            except Exception as e:  # noqa: BLE001
                self._internal_error(e)

        def _get(self):
            if not self._host_allowed():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/dashboard":
                try:
                    self._json(app.dashboard(q.get("month"), q.get("account")))
                except ToolError as e:
                    self._error(str(e))
            else:
                self._error("introuvable", HTTPStatus.NOT_FOUND)

        def _post(self):
            if not self._host_allowed():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            # Refuse les requêtes intersites : un site tiers ne doit pas piloter l'agent local.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self._error("origine refusée", HTTPStatus.FORBIDDEN)
            try:
                if url.path == "/api/chat":
                    message = (json.loads(self._body() or b"{}").get("message") or "").strip()
                    if not message:
                        return self._error("message vide")
                    self._json(app.chat(message))
                elif url.path == "/api/chat/reset":
                    self._json(app.reset())
                elif url.path == "/api/import":
                    # ?preview=1 : analyse le fichier et renvoie un aperçu sans rien enregistrer.
                    self._json(app.import_csv(self._body(), q.get("account"), dry_run=q.get("preview") == "1"))
                else:
                    self._error("introuvable", HTTPStatus.NOT_FOUND)
            except ToolError as e:
                self._error(str(e))
            except json.JSONDecodeError:
                self._error("JSON invalide")
            except anthropic.AuthenticationError:
                self._error("Clé API invalide : vérifiez ANTHROPIC_API_KEY.", HTTPStatus.BAD_GATEWAY)
            except anthropic.RateLimitError:
                self._error("Limite de requêtes atteinte, réessayez dans un instant.", HTTPStatus.TOO_MANY_REQUESTS)
            except anthropic.APIStatusError as e:
                self._error(f"Erreur de l'API ({e.status_code}) : {e.message}", HTTPStatus.BAD_GATEWAY)
            except anthropic.APIConnectionError:
                self._error("Impossible de joindre l'API Anthropic.", HTTPStatus.BAD_GATEWAY)
            except TypeError as e:
                if "authentication" not in str(e):
                    raise
                self._error("Aucun identifiant Anthropic : définissez ANTHROPIC_API_KEY.", HTTPStatus.BAD_GATEWAY)

    return Handler


def serve(tools: BudgetTools, agent_factory, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = True) -> None:
    app = App(tools, agent_factory)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(app, local_only=host in LOOPBACK_HOSTS))
    except OSError as e:
        sys.exit(f"Impossible de démarrer sur le port {port} ({e}). Essayez par ex. : agentgf --web --port 8001")
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    print(f"AgentGF — interface web sur {url}")
    print("Laissez ce terminal ouvert pendant l'utilisation (Ctrl+C pour arrêter).")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
