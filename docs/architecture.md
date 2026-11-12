# Architecture

## Vue d'ensemble

Une seule base de code (`src/tower`), une seule image Docker, **plusieurs processus**
déployés comme des services Railway distincts. Tous partagent `tower.core` et la même
base PostgreSQL.

```
                    ┌──────────────┐   ┌───────────────────┐   ┌──────────────┐
  Site public ─────▶│              │   │ hangar (worker)   │   │  intercom    │
  Extension   ─────▶│  gate (API)  │   │ services : ping,  │   │  bot Discord │
                    │  FastAPI     │◀──┼───────────────────┼───│  client HTTP │
               SSE ─┼─────────────▶│   │ ingest, radar…    │   │  de gate     │
                    │  N réplicas  │   │ N réplicas        │   │  1 réplica   │
                    └──────┬───────┘   └─────────┬─────────┘   └──────────────┘
                           │   tower.core        │       ▲ API X (radar, officielle)
                           ▼                     ▼
                    ┌─────────────────────────────────────────────────────┐
                    │ PostgreSQL : données · outbox · file de jobs ·       │
                    │ LISTEN/NOTIFY · advisory locks · erreurs             │
                    └─────────────────────────────────────────────────────┘
```

| Processus | Nom de code | Rôle | État |
|---|---|---|---|
| `tower gate` | gate | API HTTP unique : site (lecture seule), extension, bot, futurs clients. Flux SSE d'événements. | ✅ socle |
| `tower hangar` | hangar | Worker générique : exécute les jobs et les abonnements des services activés | ✅ socle |
| — | radar | Lecture des comptes suivis par l'API officielle de X (polling `since_id`, planification adaptative plafonnée par le budget) : **service hangar** `radar`, pas un processus ([radar.md](radar.md)) | ✅ partie 2 |
| `tower intercom` | intercom | Bot Discord interne ; **passe uniquement par gate** ([intercom.md](intercom.md)) | ✅ partie 2 |
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
    ports/               interfaces des dépendances externes (LLM, plateformes, web…)
    adapters/            adaptateurs partagés des ports (OpenAI, Anthropic, httpx),
                         SDK importés paresseusement
    llm/                 routeur de modèles, service d'embeddings, registre des
                         prompts (voir llm.md)
    factcheck/           contrats partagés du fact-check : schémas de sortie par rôle,
                         entrées des modèles, noms (files, jobs, topics), signaux,
                         accès paresseux (routeur, Atlas, plateformes)
    adapters/knowledge/  Atlas : Wikipédia, Wikidata, Fact Check Tools, Brave,
                         Légifrance, INSEE, cache des sources (voir fact-check.md)
    admin.py, migrations.py
    db/vector.py         type halfvec (pgvector) sans le paquet Python pgvector
  gate/                  FastAPI : app, routes, middleware, erreurs HTTP
  hangar/                worker
  services/ping/         service de démonstration (modèle à copier)
  services/ingest/       liaison des copies, maintenance des partitions et secrets
  services/radar/        lecture des comptes suivis (API officielle de X)
  services/factcheck/    déclenchement, extraction E2, assemblage E10, traductions ;
                         lot 4 : embeddings et routage E1, appariement E3, péremption,
                         revalidation, alimentation des fiches de référence
  services/investigate/  enquêtes E6 à E9, outil d'évaluation
  migrations/            Alembic (livré dans le paquet)
```

## Règles de dépendance

```
domain  ←  models / events / jobs / errors / auth  ←  usecases  ←  gate, hangar, services
```

- `core/domain` n'importe **que** la bibliothèque standard (test `test_domain_is_pure`).
- Un **service** n'importe que `tower.core`. Jamais `gate`, `intercom`, ni un autre
  service : les services communiquent par **événements** et **jobs**.
- Les **adaptateurs partagés** (OpenAI, Anthropic, embeddings, récupération web) sont
  dans `core/adapters` : un service n'importe que `tower.core`, il les y trouve. Leurs
  SDK sont importés dans les constructeurs, jamais au niveau module (test
  d'isolation des imports).
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
