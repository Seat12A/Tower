# Documentation de 12A Tower

12A Tower est le backend de **Seat 12A**, service francophone de fact-checking et de
modération centré sur X. Il sert l'API publique (site, extension Chromium), le bot
Discord interne et la surveillance de X, et héberge les services de fact-check et de
modération.

## Sommaire

| Page | Contenu |
|---|---|
| [architecture.md](architecture.md) | Vue d'ensemble, processus, flux, principes de scalabilité et de sobriété mémoire |
| [configuration.md](configuration.md) | Toutes les variables d'environnement |
| [database.md](database.md) | Tables, conventions, migrations (règles expand/contract) |
| [dossiers.md](dossiers.md) | Cycle de vie des dossiers, permissions, publication |
| [events.md](events.md) | Bus d'événements (outbox, LISTEN/NOTIFY, consommateurs, SSE) |
| [jobs.md](jobs.md) | File de travaux, worker hangar, relances, bail, maintenance |
| [services.md](services.md) | Écrire et brancher un service (fact-check, modération…) |
| [auth.md](auth.md) | Acteurs, rôles, permissions, tokens de service |
| [error-handling.md](error-handling.md) | Gestion des erreurs : codes, stockage, incidents |
| [errors.md](errors.md) | Catalogue des codes d'erreur (**généré**) |
| [logging.md](logging.md) | Logs structurés et affichage dans Railway |
| [api.md](api.md) | Référence de l'API HTTP (gate) |
| [llm.md](llm.md) | Routeur de modèles : routes, cache de contenu, budgets, allocation gratuite, prompts, adaptateurs |
| [ingestion.md](ingestion.md) | Ingestion multi-plateforme : API de l'extension, quotas, vie privée, partitions, copies, liste de suivi |
| [fact-check.md](fact-check.md) | Fact-check implémenté (lot 3) : flux, services et jobs, enquête, Atlas, confiance, quota, revue, langues, budgets, fournisseur factice, évaluation, réglages |
| [radar.md](radar.md) | Radar : lecture des comptes suivis par l'API X, planification adaptative, coûts, vérifications de l'API |
| [intercom.md](intercom.md) | Bot Discord : commandes, notifications, reprise du flux, langues, RAM |
| [deployment-railway.md](deployment-railway.md) | Déploiement et exploitation sur Railway |
| [development.md](development.md) | Environnement local, tests, commandes |
| [decisions.md](decisions.md) | Journal des décisions (retenues et écartées) |
| [roadmap.md](roadmap.md) | Prochaines étapes et lien avec le plan de fact-check |
| [factcheck/](factcheck/README.md) | Plan d'implémentation du fact-check : niveaux, pipeline, vagues de rumeurs, modèles OpenAI et Anthropic, sources intégrées, contexte, biais, plateformes et médias, données, coûts, évaluation, feuille de route |
| [sessions/](sessions/) | Résumés de chaque session de travail |

## Règle d'or

Toute modification de comportement met à jour la page concernée **dans le même
commit**. Une documentation fausse est pire qu'une documentation absente.
