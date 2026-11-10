# Architecture

## Vue d'ensemble

Une seule base de code (`src/tower`), une seule image Docker, **plusieurs processus**
déployés comme des services Railway distincts. Tous partagent `tower.core` et la même
base PostgreSQL.

```
                    ┌──────────────┐   ┌──────────────┐   ┌───────────────┐
  Site public ─────▶│              │   │              │   │               │
  Extension   ─────▶│  gate (API)  │   │   hangar     │   │ radar (futur) │
  Bot intercom ────▶│  FastAPI     │   │  (worker)    │   │ surveillance X│
  (futur)      SSE◀─│  N réplicas  │   │  N réplicas  │   │               │
                    └──────┬───────┘   └──────┬───────┘   └───────┬───────┘
                           │   tower.core (domaine, cas d'usage, bus, jobs)
                           ▼                  ▼                   ▼
                    ┌─────────────────────────────────────────────────────┐
                    │ PostgreSQL : données · outbox · file de jobs ·       │
                    │ LISTEN/NOTIFY · advisory locks · erreurs             │
                    └─────────────────────────────────────────────────────┘
```

| Processus | Nom de code | Rôle | État |
|---|---|---|---|
| `tower gate` | gate | API HTTP unique : site (lecture seule), extension, bot, futurs clients. Flux SSE d'événements. | ✅ socle |
| `tower hangar` | hangar | Worker générique : exécute les jobs et les abonnements des services activés | ✅ socle |
| `tower radar` | radar | Surveillance X (API officielle, comptes ciblés, polling `since_id`) | ⏳ à venir |
| `tower intercom` | intercom | Bot Discord interne ; **passe uniquement par gate** | ⏳ à venir |
| — | blackbox | Archive des verdicts (module du core, pas un processus) | ⏳ à venir |

## Arborescence

```
src/tower/
  cli.py                 point d'entrée unique, imports paresseux par commande
  core/
    config.py            variables d'environnement (pydantic-settings)
    logging.py           logs JSON compatibles Railway
    runtime.py           signaux, uvloop, arrêt gracieux
    db/                  moteur, sessions, Base, types PG
    domain/              PUR : dossiers, sécurité, erreurs, catalogue de codes
    models/              tables SQLAlchemy
    usecases/            seule porte d'entrée pour modifier l'état métier
    events/              outbox, listener LISTEN, consommateurs
    jobs/                file, runner, maintenance
    services/            contrat Service + registre
    errors/              capture, stockage agrégé, consultation
    auth/                tokens, résolution de l'acteur
    ports/               interfaces des dépendances externes (X, LLM, recherche…)
    admin.py, migrations.py
  gate/                  FastAPI : app, routes, middleware, erreurs HTTP
  hangar/                worker
  services/ping/         service de démonstration (modèle à copier)
  migrations/            Alembic (livré dans le paquet)
```

## Règles de dépendance

```
domain  ←  models / events / jobs / errors / auth  ←  usecases  ←  gate, hangar, services
```

- `core/domain` n'importe **que** la bibliothèque standard (test `test_domain_is_pure`).
- Un **service** n'importe que `tower.core`. Jamais `gate`, `intercom`, ni un autre
  service : les services communiquent par **événements** et **jobs**.
- **Aucun client n'accède à la base** : le bot, le site et l'extension passent par gate.
- Toute écriture métier passe par un **cas d'usage** (`core/usecases`), qui vérifie les
  permissions, applique le domaine, écrit l'audit et publie l'événement dans la même
  transaction.

## Flux type

1. Un client appelle gate : `POST /v1/dossiers/{id}/transitions`.
2. gate résout l'acteur et ouvre une transaction ; le cas d'usage verrouille la ligne,
   vérifie la transition, écrit `dossier_events` et insère `dossier.transitioned` dans
   l'outbox, puis appelle `pg_notify`.
3. Au commit, PostgreSQL délivre la notification :
   - le flux SSE de gate pousse l'événement au bot ;
   - les consommateurs des services (dans hangar) se réveillent, lisent l'outbox et
     mettent des jobs en file ;
   - hangar exécute les jobs, qui publient à leur tour des événements.

## Scalabilité

| Brique | Comment elle passe à l'échelle | Limite à surveiller |
|---|---|---|
| gate | Réplicas Railway, processus sans état | Connexions PG : `réplicas × (pool_size + max_overflow + 1)` |
| hangar | Réplicas sur la même file (`SKIP LOCKED`), ou services dédiés par file (`TOWER_QUEUES`) | Idem ; débit de la file |
| Consommateurs | Un seul réplica actif par consommateur (verrou de ligne), les autres attendent | Un handler lent bloque son consommateur : y enfiler des jobs, ne pas y travailler |
| Maintenance | Singleton par advisory lock, chaque réplica tente | — |
| SSE | Une seule connexion LISTEN par réplica, sessions SQL courtes par lot | Nombre de clients SSE par réplica |
| PostgreSQL | Vertical d'abord ; PgBouncer (mode transaction) quand les connexions saturent | `LISTEN` exige une connexion directe (hors PgBouncer transactionnel) |

Quand ajouter Redis ? Seulement sur mesure : file à plusieurs milliers de jobs par
seconde, ou fan-out temps réel à très grande échelle. Le bus et la file sont
derrière des fonctions (`publish`, `enqueue`) : le remplacement serait local.

## Sobriété mémoire

Railway facture la RAM consommée. Mesures au repos (smoke test local) :
gate ≈ 80 Mo, hangar ≈ 67 Mo.

- **Imports paresseux** : `tower hangar` ne charge ni FastAPI ni uvicorn ; un réplica
  ne charge que les services listés dans `TOWER_SERVICES`.
- **Un processus asyncio par réplica** (pas de workers uvicorn multiples) : on scale
  horizontalement.
- **Pools SQL petits** (5 par défaut, sans débordement) et une seule connexion LISTEN.
- **Tampons bornés** : erreurs (`max_pending`), cache d'authentification (1024 entrées).
- Image `python:3.13-slim`, sans dépendances de dev, `MALLOC_ARENA_MAX=2` (limite la
  fragmentation de glibc).
- Futur modèle d'embeddings local : **service hangar dédié**, pour ne pas alourdir les
  autres réplicas.
