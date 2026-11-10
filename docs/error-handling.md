# Gestion des erreurs

Sources : `core/domain/error_codes.py` (catalogue), `core/domain/errors.py`
(exceptions métier), `core/errors/` (capture et stockage), `gate/errors.py` (HTTP).

## 1. Chaque erreur a un code

Format `TWR-<DOMAINE>-<NNN>`, catalogue unique dans `error_codes.py` et page générée
[errors.md](errors.md). Chaque code définit :

| Champ | Rôle |
|---|---|
| `title` | libellé français stable |
| `http_status` | statut renvoyé par gate |
| `severity` | `info`, `warning`, `error`, `critical` : niveau de log et seuil d'incident |
| `store` | l'occurrence est enregistrée dans `error_reports` |
| `retryable` | le client ou le worker peut réessayer |
| `hint` | piste de résolution |

Règles :
- Un code n'est **jamais** renuméroté ni réutilisé. Pour le retirer : `deprecated=True`.
- Nouveau domaine : nouveau préfixe (ex. `FCK` pour le fact-check, `MOD` pour la
  modération, `RDR` pour radar, `XAPI` pour l'API X).
- Après modification : `uv run tower errors catalog --markdown > docs/errors.md`
  (un test échoue si la page est obsolète).

## 2. Erreurs métier : exceptions typées

```python
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput

raise InvalidInput("Priorité hors bornes", code=ec.DOS_BAD_PRIORITY, priority=p)
```

Classes : `NotFound`, `PermissionDenied`, `Unauthenticated`, `InvalidTransition`,
`Conflict`, `InvalidInput`. Chacune a un code par défaut ; on précise un code plus
spécifique quand il existe. Les arguments nommés deviennent `details`.

## 3. Erreurs techniques : `capture`

```python
from tower.core.errors import capture

try:
    ...
except Exception as exc:
    capture(ec.JOB_EXHAUSTED, exc, job_id=job.id)
```

`capture` est **synchrone et sans I/O** :
1. il journalise immédiatement, au niveau de la sévérité, avec `error_code`,
   `fingerprint` et le contexte (`request_id` et `job_id` inclus automatiquement) ;
2. si `store=True`, il agrège l'occurrence dans un tampon mémoire **borné** ;
3. le reporter du processus vide le tampon en base toutes les
   `TOWER_ERROR_FLUSH_INTERVAL` secondes, en une transaction, et au moment de l'arrêt.

Le contexte est nettoyé : les clés contenant `token`, `authorization`, `password`,
`secret`, `api_key` ou `cookie` sont remplacées par `[redacted]`.

## 4. Stockage agrégé : `error_reports`

Une ligne par erreur **distincte**, pas par occurrence (une mini-version de Sentry) :

- **Empreinte** = hash(code + type d'exception + dernière frame `tower.*`). Le message,
  variable, n'en fait pas partie : « post 123 » et « post 456 » donnent la même ligne.
- `occurrences`, `first_seen`, `last_seen`, dernier message, dernière trace (8 Ko),
  dernier contexte, processus.
- **Résolution** : `resolved_at`, `resolved_by`, `resolution_note`. Une erreur résolue
  qui réapparaît est **rouverte** automatiquement.
- Rétention : les erreurs résolues depuis plus de `TOWER_ERROR_RETENTION_DAYS` (90 j)
  sont purgées ; les ouvertes sont conservées.

## 5. Incidents

Pour une sévérité ≥ `error`, une **nouvelle** empreinte publie `incident.opened`, une
réapparition après résolution publie `incident.reopened`, une résolution
`incident.resolved`. Le bot pourra s'abonner à ces topics (flux SSE) pour alerter
l'équipe sur Discord, une seule fois par erreur distincte.

## 6. Côté API

Format unique :

```json
{"error": {"code": "TWR-DOS-002", "message": "Transition not allowed…",
           "details": {"allowed": ["in_review"]}, "request_id": "9f2c…"}}
```

- Validation FastAPI : `TWR-GEN-001` (422), `details.errors` par champ.
- Route inconnue : `TWR-GEN-002` (404).
- Exception imprévue : `TWR-INT-001` (500), message générique sans fuite (`"Internal error"`), avec
  `fingerprint` et `request_id` pour retrouver la trace (logs Railway, `error_reports`).
- L'en-tête `X-Request-ID` est accepté en entrée et renvoyé en sortie.

## 7. Côté jobs

`jobs.last_error` commence par le code (`TWR-JOB-002 ValueError: …`). Voir
[jobs.md](jobs.md) pour la correspondance exception → code.

## 8. Consulter et résoudre

```bash
railway run tower errors list
```

```bash
railway run tower errors resolve <empreinte> --note "corrigé dans abc123"
```

API (permission `errors:read` / `errors:manage`) : `GET /v1/errors`,
`GET /v1/errors/{empreinte}`, `POST /v1/errors/{empreinte}/resolve`,
`GET /v1/errors/catalog` (public).

Dans Railway : filtrer `@error_code:TWR-JOB-002` ou `@fingerprint:<empreinte>`.
