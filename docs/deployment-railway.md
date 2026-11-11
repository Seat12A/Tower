# Déploiement sur Railway

## Topologie

Un projet Railway, un repo, **une image** (`Dockerfile`), un service par processus :

| Service Railway | Fichier de config | Commande | Réplicas |
|---|---|---|---|
| `gate` | `railway/gate.json` | `tower gate` (+ `tower migrate` en pre-deploy) | 1 → N |
| `hangar` | `railway/hangar.json` | `tower hangar` | 1 → N |
| `Postgres` | plugin Railway | — | 1 |
| `intercom` | `railway/intercom.json` | `tower intercom` | 1 (un second réplica ne casse rien : notifications idempotentes, curseur en base) |

Le radar n'est pas un service Railway : c'est le service hangar `radar`
(`TOWER_SERVICES=ping,ingest,radar` et `TOWER_X_BEARER_TOKEN` sur le service
`hangar`). Pour l'isoler : second service hangar avec `TOWER_SERVICES=radar` et
`TOWER_QUEUES=radar`.

## Mise en place

1. Ajouter PostgreSQL au projet.
2. Créer le service `gate` depuis le repo. Dans *Settings → Config-as-code*, indiquer
   le chemin `railway/gate.json`. Variables :
   - `DATABASE_URL = ${{Postgres.DATABASE_URL}}` (URL interne, réseau privé) ;
   - `TOWER_CORS_ORIGINS`, `TOWER_LOG_LEVEL` au besoin.
   Générer un domaine public.
3. Créer le service `hangar` depuis le même repo, avec `railway/hangar.json`.
   Variables : `DATABASE_URL`, `TOWER_SERVICES=ping` (puis les vrais services).
4. Créer le premier token et le premier admin :

```bash
railway run --service gate tower create-service-token intercom --scopes dossiers:read dossiers:create dossiers:review dossiers:decide dossiers:publish events:read errors:read errors:manage watchlist:read watchlist:write usage:read
```

```bash
railway run --service gate tower staff <discord_id> --roles admin --name "Jules"
```

5. **Bot** : créer le service `intercom` depuis le même repo avec
   `railway/intercom.json`. Variables : `TOWER_DISCORD_TOKEN`, `TOWER_DISCORD_GUILD_ID`,
   les quatre `TOWER_DISCORD_CHANNEL_*`, `TOWER_GATE_URL` (adresse privée de gate,
   ex. `http://gate.railway.internal:8000`), `TOWER_GATE_TOKEN` (token créé à
   l'étape 4). Pas de `DATABASE_URL` : le bot n'accède jamais à la base.
6. **Radar** : sur le service `hangar`, `TOWER_SERVICES=ping,ingest,radar` et
   `TOWER_X_BEARER_TOKEN`. Plafond mensuel : `railway run --service gate tower llm
   budget provider:x 20` (créé à 20 $ par défaut sinon).

## Pendant un déploiement

- `preDeployCommand: tower migrate` tourne **avant** le nouveau code, pendant que
  l'ancien sert encore : migrations **expand/contract** obligatoires
  (voir [database.md](database.md)).
- Healthcheck gate sur `/health` (sans base : un incident PostgreSQL ne fait pas
  boucler les redéploiements).
- Arrêt : Railway envoie SIGTERM puis SIGKILL après `drainingSeconds` (gate 25 s,
  hangar 30 s). `TOWER_SHUTDOWN_GRACE_SECONDS` (20 s) doit rester inférieur.
- Les jobs interrompus reviennent en file ; ceux d'un worker tué sont récupérés par
  le reaper à l'expiration du bail.

## Mise à l'échelle et coûts

- Connexions PostgreSQL : `Σ réplicas × (TOWER_DB_POOL_SIZE + TOWER_DB_MAX_OVERFLOW + 1)`
  doit rester sous `max_connections` (voir la limite de l'offre Railway).
- gate : ajouter des réplicas (processus sans état).
- hangar : ajouter des réplicas, ou un service dédié par file lourde
  (`TOWER_QUEUES=deep`, `TOWER_SERVICES=factcheck`).
- RAM : un réplica ne charge que ce qu'il exécute. Ne pas activer tous les services
  partout.

## Réseau

- gate écoute sur `::` (IPv4 et IPv6), requis pour le réseau privé Railway.
- Le futur bot appellera gate via le réseau privé (`http://gate.railway.internal:<port>`).

## Exploitation

- Logs : voir [logging.md](logging.md) pour les filtres (`@level:error`, `@error_code:…`).
- Erreurs : `railway run tower errors list`.
- File : requête de supervision dans [jobs.md](jobs.md).
