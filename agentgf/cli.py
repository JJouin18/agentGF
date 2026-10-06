"""Interface en ligne de commande d'AgentGF."""

from __future__ import annotations

import argparse
import json
import sys

import anthropic

from . import __version__
from .agent import MODEL, BudgetAgent
from .db import DEFAULT_DB_PATH, connect
from .tools import BudgetTools

BANNER = f"""AgentGF {__version__} — votre assistant budget & épargne
Exemples : « J'ai payé 54,20 € de courses », « Fixe un budget loisirs de 150 € »,
« Je veux économiser 3000 € pour un voyage d'ici juin 2027 », « Fais-moi le bilan du mois ».
Tapez « quitter » pour sortir.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentgf", description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="chemin de la base SQLite")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--verbose", "-v", action="store_true", help="afficher les appels d'outils")
    parser.add_argument("message", nargs="*", help="message unique (sinon mode interactif)")
    args = parser.parse_args(argv)

    agent = BudgetAgent(BudgetTools(connect(args.db)), model=args.model, effort=args.effort)

    def on_tool(name, tool_input):
        if args.verbose:
            print(f"  ↳ {name}({json.dumps(tool_input, ensure_ascii=False)})", file=sys.stderr)

    def turn(text: str) -> bool:
        try:
            print(agent.ask(text, on_tool=on_tool))
            return True
        except anthropic.AuthenticationError:
            print("Clé API invalide : vérifiez ANTHROPIC_API_KEY.", file=sys.stderr)
        except TypeError as e:
            # Le SDK lève TypeError quand aucun identifiant n'est configuré.
            if "authentication" not in str(e):
                raise
            print("Aucun identifiant Anthropic : définissez ANTHROPIC_API_KEY.", file=sys.stderr)
        except anthropic.RateLimitError:
            print("Limite de requêtes atteinte, réessayez dans un instant.", file=sys.stderr)
        except anthropic.APIStatusError as e:
            print(f"Erreur de l'API ({e.status_code}) : {e.message}", file=sys.stderr)
        except anthropic.APIConnectionError:
            print("Impossible de joindre l'API : vérifiez votre connexion.", file=sys.stderr)
        return False

    if args.message:
        return 0 if turn(" ".join(args.message)) else 1

    print(BANNER)
    while True:
        try:
            text = input("vous › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text.lower() in {"quitter", "exit", "quit"}:
            return 0
        if text:
            turn(text)
            print()


if __name__ == "__main__":
    sys.exit(main())
