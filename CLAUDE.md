# CLAUDE.md — 12A Tower

Guide de travail pour Claude (et tout contributeur) sur ce dépôt. À lire en entier
avant de toucher au code.

## Le projet

**Seat 12A** : service francophone de fact-checking et de modération centré sur X,
identité visuelle sur le thème de l'aviation. Écosystème : site web public,
extension Chromium, bot Discord interne. Développeur principal : Jules.

**12A Tower** (ce dépôt) est le backend et le cœur de l'écosystème : API publique,
bot interne, surveillance de X, services de fact-check et de modération. Tous les
clients passent par l'API ; **le bot n'accède jamais directement à la base**.

## Langue

- **Français** : uniquement la documentation (`docs/`, `README.md`, ce fichier).
- **Anglais** pour tout le reste : code, commentaires, docstrings, messages d'erreur
  (catalogue, exceptions, réponses de l'API), messages de log, aide et sorties de la
  CLI, tags et descriptions OpenAPI, tests, migrations, fichiers de config, messages
  de commit.
- Le socle initial contient encore des commentaires et docstrings en français : les
  traduire en anglais au fil des modifications des fichiers concernés.

## Avant de commencer une tâche

1. Lire le dernier résumé dans `docs/sessions/` (où en est le projet).
2. Lire la page de `docs/` qui concerne la tâche (sommaire : `docs/README.md`).
3. Vérifier `docs/decisions.md` avant de proposer une option déjà écartée.

## Fin de session : résumé OBLIGATOIRE

**Chaque session se termine par un résumé** dans
`docs/sessions/AAAA-MM-JJ-<sujet>.md`, selon le modèle de `docs/sessions/README.md` :
objectif, réalisé, décisions, vérifications (résultats réels, y compris les échecs
et ce qui n'a pas pu être vérifié), points ouverts. Le résumé est commité avec le
travail. Une décision durable est aussi reportée dans `docs/decisions.md`.

## Documentation : toujours à jour

- `docs/` documente **précisément** chaque brique. Toute modification de comportement
  (API, table, variable, code d'erreur, topic, commande) met à jour la page concernée
  **dans le même commit**.
- `docs/errors.md` est **généré** : `uv run tower errors catalog --markdown > docs/errors.md`
  (un test échoue s'il est obsolète).
- Nouvelle variable → `docs/configuration.md` et `.env.example`. Nouvelle table →
  `docs/database.md`. Nouveau topic → `docs/events.md`. Nouvel endpoint → `docs/api.md`.

## Architecture en bref

Une base de code, une image Docker, plusieurs processus (services Railway) :

| Commande | Rôle |
|---|---|
| `tower gate` | API FastAPI (site, extension, bot), flux SSE d'événements |
| `tower hangar` | Worker générique : jobs et abonnements des services activés |
| `tower intercom` | Bot Discord interne, client de gate uniquement |

La surveillance de X est le **service hangar `radar`** (pas un processus dédié : RAM).
| `tower migrate` | Migrations (pre-deploy Railway du service gate) |

PostgreSQL est la seule infrastructure : données, outbox + LISTEN/NOTIFY, file de jobs
(`SKIP LOCKED`), advisory locks, erreurs. Détails : `docs/architecture.md`.

```
src/tower/core/domain     PUR (stdlib uniquement) : règles métier, sécurité, codes d'erreur
src/tower/core/usecases   seule porte d'entrée pour modifier l'état métier
src/tower/core/{db,models,events,jobs,errors,auth,services,ports}
src/tower/gate            HTTP      src/tower/hangar   worker
src/tower/intercom        bot Discord (client de gate, discord.py seulement dans app.py)
src/tower/services/<nom>  services enfichables (modèle : services/ping)
src/tower/migrations      Alembic (livré dans le paquet)
```

## Règles d'architecture (non négociables)

1. `core/domain` n'importe que la bibliothèque standard (test `test_domain_is_pure`).
2. Toute écriture métier passe par un **cas d'usage** qui : vérifie la permission
   (`actor.require`), applique le domaine, écrit l'audit, **publie l'événement dans la
   même transaction** (`events.publish`).
3. Un service n'importe que `tower.core`. Services, gate et bot communiquent par
   **événements** et **jobs**, jamais par import direct.
4. Les handlers de jobs sont **idempotents** ; les gestionnaires d'abonnement sont
   **courts** (ils enfilent des jobs, sans appel réseau).
5. Appels externes (API X, Anthropic, recherche) via les **ports** (`core/ports`).
6. IDs X et Discord : `BIGINT` en base, **chaînes** dans l'API et les événements.
7. Statuts en `TEXT` + `CHECK` (pas d'enum PostgreSQL).
8. Migrations **expand/contract** : compatibles avec le code déjà déployé (le
   pre-deploy tourne avant le nouveau code). Jamais de renommage ou de suppression en
   une étape.
9. Aucune publication automatique sur X. Aucun scraping. L'extension n'envoie que les
   posts filtrés, avec un payload minimal et rien sur l'utilisateur.
10. Objectivité et égalité de traitement : aucune logique de priorité ou de verdict ne
    dépend de l'auteur ni de son bord politique. Objectif ne veut pas dire neutre à
    mi-chemin : pas de faux équilibre, un fait établi (critères de
    `docs/factcheck/objectivite.md`) n'est jamais présenté comme débattu.

## Scalabilité et RAM (Railway facture la RAM)

- Processus sans état, scalés par réplicas. Un processus asyncio par réplica.
- Imports **paresseux** dans `cli.py` : un processus ne charge que ce qu'il exécute
  (hangar ne charge pas FastAPI). Ne pas ajouter d'import lourd au niveau module dans
  `core/` : le placer dans le service ou la fonction qui en a besoin.
- Pools SQL petits (`TOWER_DB_POOL_SIZE=5`), une connexion LISTEN par processus.
- Tout tampon ou cache en mémoire est **borné**.
- Une charge lourde (embeddings, enquêtes) va dans un service hangar **dédié**
  (`TOWER_SERVICES`, `TOWER_QUEUES`), pas dans tous les réplicas.

## Gestion des erreurs

Détails : `docs/error-handling.md`.

- Chaque erreur a un code stable `TWR-<DOMAINE>-<NNN>` défini dans
  `core/domain/error_codes.py` (titre, statut HTTP, sévérité, stockage, relançable,
  piste). **Ne jamais renuméroter ni réutiliser un code** (le marquer `deprecated`).
- Erreur métier : lever une exception de `core/domain/errors.py` avec le code le plus
  précis : `raise InvalidInput("…", code=ec.DOS_BAD_PRIORITY, champ=valeur)`.
- Erreur technique : `capture(ec.CODE, exc, **contexte)`, jamais un simple
  `log.error`. `capture` journalise et stocke (agrégé par empreinte dans
  `error_reports`) ; une sévérité ≥ error publie `incident.opened`.
- Jobs : `RetryLater(delay)` (temporaire), `PermanentError` (inutile de réessayer),
  sinon relance automatique avec backoff.
- Ne jamais mettre de secret dans le contexte d'une erreur ou d'un log.
- Nouveau code → régénérer `docs/errors.md`.

## Logs (Railway)

Détails : `docs/logging.md`. JSON sur une ligne, **stdout uniquement** (stderr est
affiché en rouge comme une erreur par Railway), champ `message`, niveaux
`debug/info/warn/error`. Message court et stable, données en attributs
(`log.info("job.succeeded", duration_ms=…)`). Limite Railway : 500 lignes/s par
réplica, donc pas de log dans une boucle chaude.

## Commandes

```bash
uv sync                                   # dépendances
uv run tower migrate                      # migrations
uv run tower gate                         # API (http://localhost:8000/docs)
uv run tower hangar                       # worker
uv run ruff format src tests && uv run ruff check src tests
uv run mypy                               # strict
uv run pytest -q                          # PostgreSQL réel requis (TOWER_DATABASE_URL → base tower_test)
uv run alembic revision --autogenerate -m "…"   # puis relire et nettoyer
uv run alembic check                      # modèles = migrations
uv run tower errors catalog --markdown > docs/errors.md
```

Les tests utilisent une **vraie base PostgreSQL** (par défaut
`postgresql://postgres@localhost:5433/tower_test`, modifiable via
`TOWER_DATABASE_URL`). Ne pas simuler la base : l'outbox, la file et LISTEN/NOTIFY
doivent être testés pour de vrai.

## Définition de « terminé »

- [ ] `ruff format --check`, `ruff check`, `mypy` (strict) et `pytest` verts.
- [ ] Tests ajoutés pour le nouveau comportement (cas nominal + erreurs).
- [ ] Migration générée, relue, `alembic check` OK, compatible expand/contract.
- [ ] Documentation `docs/` à jour, `docs/errors.md` régénéré si besoin.
- [ ] Résumé de session écrit dans `docs/sessions/`.
- [ ] Commit en anglais, clair, sur la branche demandée.

## Git

- Messages de commit en anglais, à l'impératif (« Add… », « Fix… »).
- Ne jamais committer `.env`, tokens ou secrets.
- Ne pas créer de pull request sans demande explicite.
