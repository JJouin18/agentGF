# AgentGF — agent IA de gestion de budget et d'épargne

AgentGF est un assistant conversationnel en français, propulsé par Claude, qui tient vos comptes à votre place :
vous lui parlez normalement (« j'ai payé 54 € de courses », « combien il me reste en loisirs ? »),
et il enregistre, analyse et vous conseille à partir de **vos données stockées localement** (SQLite).

## Fonctionnalités

| Domaine | Ce que l'agent sait faire |
|---|---|
| Transactions | Ajouter revenus/dépenses, lister/filtrer, supprimer, importer un relevé CSV |
| Budgets | Plafond mensuel par catégorie, suivi du consommé/restant, alerte de dépassement |
| Analyses | Bilan mensuel (solde, taux d'épargne, répartition), tendances sur N mois |
| Épargne | Objectifs avec échéance et effort mensuel nécessaire, versements/retraits |
| Simulation | Projection à intérêts composés (versement mensuel, taux annuel, durée) |

## Installation

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
```

## Utilisation

```bash
agentgf                      # mode interactif
agentgf -v "Fais-moi le bilan du mois"   # message unique, appels d'outils affichés
agentgf --db ./mon-budget.db # base personnalisée (défaut : ~/.agentgf/budget.db, ou $AGENTGF_DB)
```

Exemple de session :

```
vous › Je touche 2400 € de salaire par mois, fixe-moi un budget alimentation de 350 €
vous › J'ai dépensé 62,30 € chez Carrefour hier
vous › Je veux mettre 3000 € de côté pour un voyage avant juin 2027
vous › Si je place 150 € par mois à 3 % pendant 10 ans, j'aurai combien ?
```

### Import CSV

Colonnes attendues (séparateur `,` `;` ou tabulation, décimales `,` ou `.`) : `date` (AAAA-MM-JJ),
`montant` (négatif = dépense), `categorie`, `description`.

## Architecture

```
agentgf/
├── agent.py   # boucle agentique Claude (tool use, thinking adaptatif, fallback sur refus)
├── tools.py   # outils métier + schémas JSON stricts exposés au modèle
├── db.py      # schéma SQLite
└── cli.py     # interface en ligne de commande
```

L'agent utilise `claude-opus-5-5` (modifiable via `--model`), effort `medium` par défaut (`--effort`),
et le paramètre `fallbacks: "default"` pour qu'une requête refusée soit reprise automatiquement par un modèle de repli.

## Tests

```bash
pytest
```

Les tests couvrent les outils métier et la boucle agentique (avec un client simulé, sans appel réseau).

> AgentGF fournit des repères pédagogiques, pas un conseil en investissement personnalisé.
