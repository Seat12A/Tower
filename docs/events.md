# Bus d'événements

Source : `src/tower/core/events/`.

## Principe : outbox transactionnelle

Un événement est une ligne de la table `events`, écrite **dans la même transaction**
que le changement d'état qu'il décrit, suivie d'un `pg_notify('tower_events', topic)`.

- Rollback : ni l'événement ni la notification n'existent.
- Commit : PostgreSQL délivre la notification à tous les processus en `LISTEN`.
- La notification n'est qu'un **réveil** : la vérité est dans la table. Un
  polling de secours (`TOWER_JOB_POLL_INTERVAL`) couvre les notifications perdues.

```python
from tower.core.events import publish

async with db.transaction() as session:
    ...  # changement d'état
    await publish(session, "dossier.transitioned", payload,
                  aggregate_type="dossier", aggregate_id=public_id, actor=actor.label)
```

## Lecture sans trou : `(xid, id)`

Lire « `id` > dernier vu » est faux : une transaction ouverte peut détenir un `id`
plus petit qu'un événement déjà validé, qui serait alors sauté pour toujours.

Chaque événement stocke `xid = pg_current_xact_id()`. Le lecteur :

1. ne garde que `xid < pg_snapshot_xmin(pg_current_snapshot())`, c'est-à-dire des
   transactions toutes terminées ;
2. trie par `(xid, id)` et avance un curseur `(xid, id)`.

Tout événement qui deviendra visible plus tard aura un `xid` supérieur au seuil
courant, donc au curseur. Couvert par `test_open_transaction_hides_later_commits`.

Le curseur public est la chaîne `"<xid>-<id>"` (opaque pour les clients).

## Consommateurs nommés (dans hangar)

`consume_batch` traite un lot dans **une transaction** : verrou de la ligne
`event_cursors` (`FOR UPDATE SKIP LOCKED`), lecture, gestionnaire, avance du curseur.

- Les effets en base du gestionnaire et l'avance du curseur sont atomiques.
- Si le gestionnaire échoue, le lot est rejoué (`TWR-EVT-002`, stocké).
- Un seul réplica traite un consommateur à la fois ; les autres sautent sans erreur.
- **Règle** : un gestionnaire doit être court. Il met des jobs en file, il ne fait
  pas d'appels réseau.

## Flux SSE pour les clients (gate)

`GET /v1/events` (permission `events:read`), voir [api.md](api.md).

- Reprise par l'en-tête `Last-Event-ID` (EventSource l'envoie automatiquement) ou
  par `?after=<curseur>`. `after=0` : depuis le début ; défaut : depuis maintenant.
- Filtre `?topics=dossier.opened&topics=incident.opened`.
- Keepalive toutes les `TOWER_SSE_HEARTBEAT_SECONDS`.
- Aucune connexion SQL n'est tenue par client : une session courte par lot, réveil
  par la connexion LISTEN partagée du réplica.

### Curseurs stockés des clients de service

Un client de service (le bot) peut stocker sa position côté gate pour reprendre sans
perte après un redémarrage : `GET` et `PUT /v1/events/cursors/{nom}` (permission
`events:read`, voir [api.md](api.md)). Le curseur est rangé dans `event_cursors` sous
la clé `sse:<client de service>:<nom>` ; un membre de l'équipe qui agit via le bot
utilise les curseurs du bot. Nom : 1 à 64 caractères `a-z 0-9 . _ -` (`TWR-EVT-003`).
Aucun audit ni événement : c'est un état de lecture du client, et un événement serait
lui-même renvoyé dans le flux. Livraison au moins une fois : le client dédoublonne par
identifiant d'événement.

## Topics existants

| Topic | Émis par | Payload |
|---|---|---|
| `dossier.opened` | cas d'usage `open_dossier` | état du dossier |
| `dossier.transitioned` | `transition`, `mark_published` | état + `from`, `to`, `reason` |
| `incident.opened` | reporter d'erreurs (sévérité ≥ error, nouvelle empreinte) | code, titre, sévérité, processus, lieu, message |
| `incident.reopened` | reporter (erreur résolue qui réapparaît) | idem |
| `incident.resolved` | `errors.service.resolve` | empreinte, code, note |
| `ping.requested` / `ping.done` | service ping | démonstration |
| `content.ingested` | cas d'usage `ingest_items`, **nouveau contenu seulement** (jamais par observation) | `id` (clé interne en chaîne), `public_id`, `platform`, `external_id`, `lang`, `source` |
| `content.verified` | un contenu déclaré par l'extension devient de confiance : intégration publique conforme (`record_verification`, `source` = `oembed`) ou correction par une lecture de la plateforme (`ingest_items`, `source` = `radar`, `manual` ou `feed`). Voir [ingestion.md](ingestion.md#vérification-des-contenus) | idem `content.ingested` |
| `watchlist.changed` | cas d'usage de la liste de suivi | `action` (`added`, `updated`, `resolved`), compte suivi (`external_id` nul tant qu'un compte ajouté par pseudo n'est pas résolu par le radar), (`public_id`, `platform`, `external_id`, `handle`, `category`, `priority`, `active`, `version`) |
| `extension_filters.published` | `publish_filters` | `version` |
| `budget.threshold_reached` | registre des budgets (`core/budget.py` : routeur de modèles et radar), au premier franchissement de 50, 80 ou 100 % d'une portée sur la période | `scope`, `period_start`, `threshold_pct`, `spent_usd_micros`, `cap_usd_micros` |
| `claim.extracted` | cas d'usage `record_extraction` (service `factcheck`, E2), un par contenu | `content_id` (clé interne en chaîne), `content_public_id`, `platform`, `external_id`, `level`, `claim_ids`, `group_ids` |
| `investigation.requested` | `record_extraction`, promotion N4 | `investigation_id`, `group_id`, `level`, `depth`, `content_id` |
| `investigation.completed` | `complete_investigation` / `fail_investigation` (service `investigate`) | `investigation_id`, `group_id`, `level`, `depth`, `verdict_id`, `veracity`, `context_need`, `outcome` (`drafted`, `unverifiable_at_framing`, `failed`) |
| `draft.ready` | cas d'usage `open_draft` (service `factcheck`, E10) | `draft_id` (= identifiant public du verdict), `dossier_id`, `group_id`, `level`, `veracity`, `context_need`, `confidence`, `platform`, `external_id`, `url` |
| `draft.reviewed` | cas d'usage `review_draft` | `draft_id`, `dossier_id`, `decision`, `outcome` (`endorsed`, `validated`, `rejected`), `final_veracity`, `labels`, `reviewer` |
| `verdict.published` | `review_draft`, à la validation (dossier `validated` ; la publication sur X reste manuelle) | `verdict_id`, `group_id`, `dossier_id`, `veracity`, `context_need`, `platform`, `external_id`, `retranslate_from` |
| `claim_group.created` | `record_extraction` (E3), nouveau groupe | `group_id`, `claim_id`, `level`, `content_id`, `rule` (règle d'E3), `related_group_id` (groupe proche suggéré pour une fusion manuelle, ou nul), `reference_fact_id`, `band`, `band_side` |
| `claim_group.merged` | cas d'usage `merge_groups` (gate, bot) | `target_group_id`, `source_group_id`, `claim_ids` (affirmations déplacées : une scission de ces affirmations annule la fusion), `actor`, `reason` |
| `claim_group.split` | cas d'usage `split_group` | `group_id`, `new_group_id`, `claim_ids`, `actor`, `reason` |
| `claim_group.level_changed` | `record_extraction` (contenu de niveau supérieur), forçage de niveau, ordonnanceur E4 (`change_level`), mode vague | `group_id`, `from_level`, `to_level`, `reason` (`content`, `override`, `request`, `promotion`, `demotion`, `wave`) ; `unconfirmed: true` pour une promotion sans client de plateforme pour confirmer les métriques |
| `verdict.reused` | `record_extraction` (affirmation rattachée à un groupe couvert, ou héritage par bande), job `factcheck.revalidate` | `verdict_id` (verdict qui couvre), `group_id`, `claim_id`, `content_id`, `rule` (`identical_signature`, `band`), `origin_verdict_id` (verdict d'origine pour une bande) |
| `verdict.expired` | tâche périodique `factcheck:expire` | `verdict_id`, `group_id`, `claim_type`, `expires_at` |
| `reference_fact.validated` | cas d'usage `validate_reference_fact` | `fact_id`, `indicator`, `territory`, `period_start`, `period_end`, `validated_by` |
| `verdict_band.validated` | cas d'usage `validate_band` (brouillon, bot, API) | `band_id`, `fact_id`, `band`, `side`, `veracity`, `validated_by` |
| `processing_profile.activated` | cas d'usage `activate_profile`, `rollback_profile` | `version`, `previous_version`, `actor`, `reason`, `changes` (`{chemin: [ancien, nouveau]}`) |
| `level_override.created` | cas d'usage `create_override` | `override_id`, `target_type`, `target_id`, `level`, `expires_at`, `actor`, `reason` |
| `narrative.surge_detected` | cas d'usage `change_state` des récits, à l'entrée en `surge` ou `alert` (publié une seconde fois, `tier = alert`, quand une vague monte au palier d'alerte) | `narrative_id`, `tier` (`surge`, `alert`), `share`, `accounts`, `platforms` (liste de noms), `title` (nul tant qu'il n'y en a pas), `representative_group_id` |
| `narrative.surge_ended` | `change_state`, après `TOWER_FC_SURGE_END_HOURS` sous le palier `watch` | `duration_s`, `posts`, `platforms`, `cost_usd_micros` (enquêtes des groupes du récit), `first_draft_delay_s` (nul sans brouillon) ; agrégat `narrative` = identifiant public du récit |
| `narrative.merged` | cas d'usage `merge_narratives` (gate, bot) | `target_id`, `source_id`, `group_ids`, `actor`, `reason` |
| `narrative.split` | cas d'usage `split_narrative` | `narrative_id`, `new_narrative_id`, `group_ids`, `actor`, `reason` |
| `source_registry.changed` | cas d'usage du registre (`create_entry`, `update_entry`, `deactivate_entry`, `apply_control_seed`) : API et bot | `registry_id`, `domain`, `change` (`created`, `updated`, `deactivated`), `version`, `tier`, `controlled_by`, `changed_by` |
| `country_registry.changed` | cas d'usage `upsert_jurisdiction_source`, `upsert_legal_portal`, `upsert_election` | `table`, `id`, `country`, `change` |

Convention de nommage : `<agrégat>.<verbe au participe passé>`.

## Rétention

Purge des événements de plus de `TOWER_EVENT_RETENTION_DAYS` (30 jours) par la
maintenance. Un consommateur en retard de plus de 30 jours perdrait des événements :
surveiller `event_cursors.updated_at`.
