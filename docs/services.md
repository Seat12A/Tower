# Services enfichables

Un **service** regroupe une capacité métier : fact-check, modération, ingestion,
surveillance… Il est **déclaratif** : il annonce ses jobs et ses abonnements, et
hangar les exécute. Source : `src/tower/core/services/`.

## Contrat

```python
from tower.core.services import JobContext, JobSpec, Service, Subscription

async def on_dossier_opened(session, events):        # court, transactionnel
    for e in events:
        await enqueue(session, "moderation", "moderation.analyze",
                      {"dossier": e.aggregate_id}, dedupe_key=f"mod:{e.aggregate_id}")

async def analyze(ctx: JobContext) -> None:          # le vrai travail
    async with ctx.db.transaction() as session:
        ...
    ctx.log.info("moderation.analyzed")

service = Service(
    name="moderation",
    description="Modération de contenus X",
    jobs=(JobSpec("moderation.analyze", analyze, queue="moderation", timeout=120),),
    subscriptions=(Subscription("dossiers", ("dossier.opened",), on_dossier_opened),),
)
```

### Tâches périodiques singleton

`Service.periodic` déclare des tâches `PeriodicTask(name, every_seconds, fn)` :
`fn(session, settings) -> int` s'exécute dans une transaction, sur un seul réplica à la
fois (verrou consultatif `pg_try_advisory_xact_lock(hashtext(name))`), aux côtés de la
maintenance du socle. Exemple : partitions et secrets du service `ingest`.

## Brancher un nouveau service

1. Créer `src/tower/services/<nom>/__init__.py` qui expose `service`.
2. L'ajouter à `BUILTIN_SERVICES` dans `core/services/registry.py`.
3. L'activer sur un réplica : `TOWER_SERVICES=ping,<nom>`.
4. Documenter ses topics dans [events.md](events.md) et ses codes d'erreur dans le
   catalogue (domaine dédié, ex. `TWR-FCK-001`), puis régénérer [errors.md](errors.md).

## Règles

- Un service n'importe que `tower.core` (jamais gate, intercom ni un autre service).
- Les types de job sont préfixés par le service (`factcheck.triage`) ; les doublons
  sont refusés au démarrage.
- Les appels externes (API X, Anthropic, recherche) passent par les **ports**
  (`core/ports`) : interfaces remplaçables par des doublures en test.
- Les handlers de jobs sont **idempotents** (voir [jobs.md](jobs.md)).
- Les gestionnaires d'abonnement sont **courts** : ils lisent des événements et
  enfilent des jobs, sans appel réseau.
- Erreurs : lever `RetryLater` (quota, indisponibilité temporaire), `PermanentError`
  (inutile de réessayer), ou laisser remonter (relance automatique). Pour un
  incident métier à signaler sans échouer : `capture(code, exc, ...)`.

## Services prévus

| Service | Rôle | Notes |
|---|---|---|
| `ping` | Démonstration et supervision (`ping.requested` → `ping.done`) | ✅ |
| `ingest` | Liaison des copies, partitions des observations, secrets d'installation (l'endpoint de l'extension est dans gate) | ✅ file `ingest`, voir [ingestion.md](ingestion.md) |
| `radar` | Lecture des comptes suivis par l'API officielle de X (puis Bluesky) : planification adaptative, budget `provider:x`, résolution des pseudos | ✅ file `radar`, voir [radar.md](radar.md) |
| `factcheck` | Déclenchement N3, extraction E2, demande manuelle N4, assemblage E10 (confiance, revérification, quota, dossier), traductions à la validation ; plus tard E0 à E5, ordonnanceur, vagues | ✅ (lot 3) file `fc-fast`, voir [fact-check.md](fact-check.md) |
| `investigate` | Enquêtes (E6 à E9) : cadrage, collecte (Atlas), jugement, second avis, objection, escalade, rédaction | ✅ (lot 3) file `fc-deep`, réplica dédié possible, voir [fact-check.md](fact-check.md) |
| `atlas` | Synchronisation des sources de connaissance (miroir LEGI, actualité, ClaimReview, registre) | voir [factcheck/connaissances.md](factcheck/connaissances.md) |
| `media` | Empreintes, OCR, transcription, provenance des médias | futur |
| `moderation` | Modération de contenus X | à définir |
