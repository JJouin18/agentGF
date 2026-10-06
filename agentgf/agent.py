"""Boucle agentique : Claude converse avec l'utilisateur et appelle les outils budget/épargne."""

from __future__ import annotations

from datetime import date

import anthropic

from .tools import TOOL_SCHEMAS, BudgetTools, ToolError

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 25

SYSTEM_PROMPT = """Tu es AgentGF, un assistant personnel de gestion de budget et d'épargne. \
Tu réponds en français, de façon claire et concise, avec les montants en euros.

Tu disposes d'outils qui lisent et écrivent dans la base locale de l'utilisateur : \
comptes (courant, livrets...), transactions, plafonds de budget par catégorie, règles de catégorisation, \
objectifs d'épargne et simulations d'intérêts composés.

Principes :
- Quand l'utilisateur mentionne une dépense ou un revenu, enregistre-le directement \
(catégorie en minuscules, réutilise les catégories existantes quand c'est pertinent). \
Sans compte précisé, utilise le compte courant par défaut.
- Un mouvement entre deux comptes de l'utilisateur (ex. alimenter son livret) est un virement interne : \
utilise l'outil transfer, jamais une dépense + un revenu.
- Après un import, s'il reste des transactions « a_categoriser », classe-les toi-même d'après leur libellé \
(list_uncategorized puis recategorize_transactions). Quand un même commerçant revient souvent, \
crée une règle avec add_category_rule pour les prochains imports. Demande à l'utilisateur seulement \
pour les libellés vraiment ambigus.
- Appuie chaque analyse sur les données des outils ; n'invente jamais de chiffres.
- Signale les dépassements de budget et propose des pistes concrètes d'économies.
- Pour l'épargne, rappelle les bonnes pratiques (épargne de précaution de 3 à 6 mois de dépenses, \
se verser l'épargne en début de mois, règle 50/30/20 comme repère) sans être moralisateur.
- Avant une suppression, assure-toi que l'élément visé est bien celui que l'utilisateur veut supprimer.
- Tu donnes des repères pédagogiques, pas de conseil en investissement personnalisé : \
pour des placements à risque ou une situation complexe, suggère de consulter un conseiller."""


class BudgetAgent:
    def __init__(self, tools: BudgetTools, client: anthropic.Anthropic | None = None,
                 model: str = MODEL, effort: str = "medium"):
        self.tools = tools
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.messages: list = []
        self.system = SYSTEM_PROMPT + f"\n\nDate du jour : {date.today().isoformat()}."

    def _call(self):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=self.system,
            tools=TOOL_SCHEMAS,
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def ask(self, user_input: str, on_tool=None) -> str:
        """Envoie un message utilisateur et exécute les outils jusqu'à la réponse finale.

        Si l'appel API échoue, l'historique est restauré à son état d'avant le message.
        """
        checkpoint = len(self.messages)
        try:
            return self._ask(user_input, on_tool)
        except Exception:
            del self.messages[checkpoint:]
            raise

    def _ask(self, user_input: str, on_tool) -> str:
        self.messages.append({"role": "user", "content": user_input})

        for _ in range(MAX_TOOL_ROUNDS):
            response = self._call()
            # Historique en ajout seul : on renvoie le contenu complet (thinking, fallback, tool_use).
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return "Désolé, je ne peux pas traiter cette demande."
            if response.stop_reason != "tool_use":
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    text += "\n\n[réponse tronquée]"
                return text

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if on_tool:
                    on_tool(block.name, block.input)
                try:
                    content, is_error = self.tools.run(block.name, dict(block.input)), False
                except ToolError as e:
                    content, is_error = f"Erreur : {e}", True
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})

        return "Je me suis arrêté : trop d'appels d'outils successifs pour cette demande."
