# Configuration

Source : `src/tower/core/config.py`. Préfixe `TOWER_`, sauf les variables injectées
par Railway. Un fichier `.env` est lu en local (voir `.env.example`).

## Général

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_ENV` | `dev` | `dev`, `test` ou `prod` (l'image Docker fixe `prod`) |
| `TOWER_PROCESS` | fixé par la CLI | Nom du processus, ajouté à chaque log |
| `TOWER_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `TOWER_LOG_FORMAT` | `console` en dev, `json` sinon | Rendu des logs |

## Base de données

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_DATABASE_URL` ou `DATABASE_URL` | `postgresql://postgres@localhost:5432/tower` | URL PostgreSQL. `postgres://` est accepté. `sslmode` est traduit pour asyncpg |
| `TOWER_DB_POOL_SIZE` | `5` | Connexions du pool par réplica |
| `TOWER_DB_MAX_OVERFLOW` | `0` | Connexions supplémentaires temporaires |
| `TOWER_DB_POOL_TIMEOUT` | `10` | Attente maximale d'une connexion (s) |
| `TOWER_DB_STATEMENT_TIMEOUT_MS` | `30000` | `statement_timeout` côté serveur |

## gate

| Variable | Défaut | Description |
|---|---|---|
| `PORT` / `TOWER_PORT` | `8000` | Port d'écoute (Railway injecte `PORT`) |
| `TOWER_HOST` | `::` | Écoute IPv4 et IPv6 (requis par le réseau privé Railway). En local sans IPv6 : `0.0.0.0` |
| `TOWER_AUTH_CACHE_TTL` | `15` | Durée du cache des tokens et rôles (s). Délai maximal d'une révocation |
| `TOWER_SSE_HEARTBEAT_SECONDS` | `15` | Intervalle des commentaires keepalive SSE |
| `TOWER_SSE_BATCH_SIZE` | `200` | Événements lus par requête SQL |
| `TOWER_CORS_ORIGINS` | vide | Origines autorisées (GET uniquement), séparées par des virgules |

## hangar

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_SERVICES` | `ping` | Services activés, séparés par des virgules |
| `TOWER_QUEUES` | vide | Files consommées ; vide = toutes celles des services activés |
| `TOWER_WORKER_CONCURRENCY` | `4` | Jobs simultanés par réplica |
| `TOWER_JOB_LEASE_SECONDS` | `300` | Bail d'un job réservé ; au-delà, le reaper le récupère |
| `TOWER_JOB_POLL_INTERVAL` | `5` | Polling de secours si une notification est perdue (s) |
| `TOWER_SHUTDOWN_GRACE_SECONDS` | `20` | Temps laissé aux jobs en cours après SIGTERM. Doit être < `drainingSeconds` Railway |

## Rétention et erreurs

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_JOB_RETENTION_DAYS` | `7` | Purge des jobs terminés |
| `TOWER_EVENT_RETENTION_DAYS` | `30` | Purge de l'outbox |
| `TOWER_ERROR_RETENTION_DAYS` | `90` | Purge des erreurs **résolues** |
| `TOWER_ERROR_FLUSH_INTERVAL` | `5` | Vidage du tampon d'erreurs en base (s) |

## Lues telles quelles (Railway)

`RAILWAY_REPLICA_ID` (ajouté aux logs, sert d'identifiant de worker),
`RAILWAY_DEPLOYMENT_ID`.
