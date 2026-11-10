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
| `ingest` | Contenus de toutes plateformes (`content_items`, `observations`), endpoint de l'extension | à venir, voir [factcheck/plateformes-medias.md](factcheck/plateformes-medias.md) |
| `radar` | Polling de l'API X sur `watched_accounts` (puis flux Bluesky) | processus dédié prévu |
| `factcheck` | Étages rapides et par lots du fact-check (E0 à E5, E10, E11), ordonnanceur, vagues | voir [factcheck/pipeline.md](factcheck/pipeline.md) |
| `investigate` | Enquêtes (E6 à E9) : cadrage, collecte, jugement, rédaction | idem |
| `atlas` | Synchronisation des sources de connaissance (miroir LEGI, actualité, ClaimReview, registre) | voir [factcheck/connaissances.md](factcheck/connaissances.md) |
| `media` | Empreintes, OCR, transcription, provenance des médias | futur |
| `moderation` | Modération de contenus X | à définir |
