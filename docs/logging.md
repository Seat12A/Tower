# Logs (Railway)

Source : `src/tower/core/logging.py`.

## Ce que Railway attend

D'après la [documentation Railway](https://docs.railway.com/guides/logs) :

- un log structuré est un **objet JSON sur une seule ligne** ;
- `message` contient le texte ; `level` vaut `debug`, `info`, `warn` ou `error` ;
- toute autre clé de premier niveau devient un **attribut filtrable** ;
- **stdout** est classé `info`, **stderr** est classé `error` et affiché en rouge ;
- limite : **500 lignes par seconde et par réplica**, au-delà les logs sont perdus.

## Ce que fait la Tower

- structlog rend chaque log en JSON (orjson) sur une ligne, **sur stdout uniquement**.
- `event` est renommé en `message` ; `warning` devient `warn`, `critical` devient `error`.
- Champs ajoutés partout : `ts` (ISO UTC), `logger`, `process` (gate, hangar…),
  `replica` (8 premiers caractères de `RAILWAY_REPLICA_ID`).
- Contexte lié automatiquement : `request_id` (gate), `job_id` (hangar).
- Erreurs : `error_code`, `error_title`, `fingerprint`, et la trace dans `exception`
  (sur la même ligne JSON, Railway l'affiche sur plusieurs lignes).
- uvicorn, SQLAlchemy et Alembic passent par le même rendu ; le log d'accès d'uvicorn
  est remplacé par le nôtre (`http_method`, `http_path`, `http_status`, `duration_ms`).
- `/health` et `/ready` ne sont pas journalisés (le healthcheck consommerait le quota).
- En local (`TOWER_ENV=dev`), rendu console lisible et coloré.

Exemple :

```json
{"http_method":"POST","http_path":"/v1/dossiers","http_status":201,"duration_ms":34.8,"request_id":"55b5dead2a0642a3","level":"info","logger":"tower.gate.access","ts":"2026-11-10T19:14:55.551421Z","process":"gate","message":"POST /v1/dossiers 201"}
```

## Filtres utiles dans Railway

| Besoin | Filtre |
|---|---|
| Toutes les erreurs | `@level:error` |
| Un code précis | `@error_code:TWR-JOB-002` |
| Une erreur agrégée | `@fingerprint:4d6570bb3ea6f34d` |
| Suivre une requête | `@request_id:55b5dead2a0642a3` |
| Suivre un job | `@job_id:1234` |
| Requêtes lentes | `@duration_ms:>500` |
| Erreurs serveur | `@http_status:>=500` |
| Un processus | `@process:hangar` |

## Bonnes pratiques

- Message court et stable (`job.succeeded`, `POST /v1/dossiers 201`), données dans
  les attributs, jamais concaténées dans le message.
- Pas de log dans une boucle chaude ; agréger plutôt (limite de 500 lignes/s).
- Jamais de secret ni de donnée personnelle d'utilisateur de l'extension.
- Une erreur passe par `capture(code, exc)`, pas par `log.error` : elle obtient un
  code, une empreinte et, si besoin, un stockage.
