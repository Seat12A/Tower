# Radar : lecture des comptes suivis

Le radar lit les nouveaux posts des comptes de la liste de suivi par l'**API officielle**
de la plateforme et les donne à l'ingestion générique (source `radar`). Il ne publie
rien et ne fait aucun scraping. Seul X est branché ; rien n'est propre à X hors de
l'adaptateur (`core/adapters/x_client.py`) et du choix du client par plateforme.

**Service hangar `radar`**, et non processus dédié : un processus de plus coûte environ
70 Mo de RAM, un service hangar quelques Mo. Il peut tourner sur un réplica dédié
(`TOWER_SERVICES=radar`, `TOWER_QUEUES=radar`). Source : `src/tower/services/radar/`.

## Briques

| Brique | Rôle |
|---|---|
| `core/ports/platforms.py` | Port `PlatformClient` / `PollingPlatformClient` (`poll_page`, prix par ressource), `ReadUsage`, `PollPage`, erreurs typées `PlatformAuthFailed`, `PlatformRateLimited`, `PlatformUnavailable`, `PlatformBadResponse`, `PlatformNotConfigured` |
| `core/adapters/x_client.py` | `XPlatformClient` : API X v2, jeton d'application (bearer), httpx importé paresseusement |
| `core/domain/radar.py` | Calculs purs : intervalle de base, intervalle suivant, plafond quotidien de lectures |
| `core/budget.py` | Réservation et règlement du coût (portée `provider:x`), voir [llm.md](llm.md#budgets) |
| `services/radar` | Tâche `radar:schedule`, jobs `radar.poll_account` et `radar.resolve_account`, abonnement à `watchlist.changed` |

## Déroulé

```
radar:schedule (60 s, singleton)                watchlist.changed (external_id nul)
   │ comptes dus, par priorité,                     │ abonnement court
   │ dans la limite du plafond du jour               ▼
   ▼                                          radar.resolve_account
radar.poll_account (file radar, dedupe par compte)   │ lecture d'utilisateur (0,010 $)
   │ réserve max_results × 0,005 $                   ▼
   │ GET /2/users/{id}/tweets?since_id=…        resolve_watched_account
   │ règle le coût réel, ingère (source radar)       → watchlist.changed (resolved)
   ▼
content.ingested (nouveaux contenus) ; since, intervalle et next_poll_at mis à jour
```

### Planification adaptative

- **Comptes dus** : actifs, de la plateforme, avec identifiant, `next_poll_at` nul
  (nouveau) ou dépassé ; tri par priorité décroissante puis ancienneté.
- **Plafond quotidien** de lectures = budget restant du mois (`cap - spent - reserved`
  de `provider:x`) ÷ jours restants (aujourd'hui compris) ÷ coût moyen d'une lecture
  (`TOWER_RADAR_EXPECTED_POSTS_PER_POLL` × prix d'un post ; une lecture vide ne coûte
  rien). Les lectures déjà enfilées aujourd'hui (jobs `radar.poll_account` créés depuis
  minuit UTC) sont déduites. Au plus `TOWER_RADAR_SCHEDULE_BATCH` jobs par passage.
- Si la portée `provider:x` n'a encore aucun compteur, la planification le crée avec
  `TOWER_X_MONTHLY_CAP_USD` (20 $) : le radar n'est **jamais** sans plafond.
- À l'enfilage, `next_poll_at` est repoussé d'un intervalle (bail) : un job en échec
  n'est pas réenfilé à chaque passage.
- **Intervalle** : base selon la priorité (≥ 50 : 2 min ; 0 à 49 : 10 min ; < 0 :
  30 min), divisé par `1 + n // 2` quand une lecture ramène `n` posts, doublé à chaque
  lecture vide consécutive, borné entre 1 min et 6 h. Bornes en configuration.
- La priorité ne dépend que de la décision de l'équipe, jamais de l'auteur ni de son
  bord (règle 10) ; elle ne change que la latence.

### Lecture d'un compte (`radar.poll_account`)

1. Compte inactif, sans identifiant ou plateforme non branchée : rien.
2. Pour chaque page (au plus `TOWER_RADAR_MAX_PAGES`, une seule à la première lecture,
   sans historique) : réservation de `max_results × prix d'un post` sur `provider:x`,
   appel avec `since_id`, `max_results`, `exclude` (`retweets` par défaut),
   `pagination_token`.
3. Une seule transaction finale : règlement du coût réel (posts renvoyés, y compris
   ceux des `includes`), ingestion par le cas d'usage `ingest_items` (une observation
   par post, source `radar`, acteur `system:radar`), mise à jour de `since_cursor`,
   `last_polled_at`, `last_post_at`, `consecutive_empty`, `poll_interval_s`,
   `next_poll_at`.
4. **Idempotence** : tout est écrit dans la même transaction ; un job rejoué avant
   validation relit la même fenêtre (X ne refacture pas une ressource relue dans les
   24 h UTC), un job rejoué après validation part du nouveau `since` et ne trouve rien.

| Situation | Effet |
|---|---|
| Budget épuisé (`BudgetRefused`) | `RetryLater` jusqu'au début du mois suivant, aucun appel |
| 429 | Réservation réglée à 0, `RetryLater` jusqu'à `x-rate-limit-reset` |
| 401 / 403 | `TWR-RAD-001` stocké, `PermanentError` |
| 404 (compte disparu) | `TWR-RAD-006` stocké, prochaine lecture à l'intervalle maximal |
| 5xx, réseau, délai | `TWR-RAD-003`, relance automatique avec recul |
| Jeton absent | `TWR-RAD-007` stocké, `PermanentError` |
| Réponse inattendue | `TWR-RAD-004` stocké, `PermanentError` |

Le radar écrit ses colonnes de planification en SQL direct, sans cas d'usage ni
événement ni incrément de `version` : donnée opérationnelle dérivée, comme la liaison
des copies (voir [decisions.md](decisions.md)).

### Résolution d'un compte ajouté par pseudo

L'abonnement `radar.watchlist` enfile `radar.resolve_account` (`dedupe_key` par compte)
pour chaque `watchlist.changed` actif dont `external_id` est nul. Le job réserve le
prix d'une lecture d'utilisateur, appelle `get_account(pseudo)`, règle le coût réel,
puis appelle `resolve_watched_account` (voir [ingestion.md](ingestion.md#liste-de-suivi--v1watched-accounts)).
Pseudo introuvable : `TWR-RAD-006` stocké (visible par `/incidents`). Identifiant déjà
suivi : avertissement journalisé, rien n'est modifié.

## Coûts

Tarif X au paiement à l'usage (vérifié le 2026-11-11, page « Pricing » de docs.x.com) :
**0,005 $ par post lu, 0,010 $ par utilisateur lu**, facturé **par ressource
renvoyée**, ressources dédoublonnées sur une fenêtre de 24 h UTC (garantie « souple »),
plafond de **3 millions** de lectures de posts par cycle mensuel. Une réponse vide ne
coûte donc rien. Les prix sont en configuration (`TOWER_X_PRICE_*`), le plafond
mensuel en base (`provider:x`, `tower llm budget provider:x <usd>`).

Ordre de grandeur : 100 comptes publiant 5 posts par jour = 500 posts/jour ≈ 2,50 $ par
jour, 75 $ par mois ; avec le plafond par défaut de 20 $, le plafond quotidien limite
les lectures et les comptes de faible priorité sont lus moins souvent.

## Vérifications de l'API X (2026-11-11)

Faites sur la spécification OpenAPI officielle (`https://api.x.com/2/openapi.json`,
version 2.174) et docs.x.com (pages « Pricing » et « Rate limits ») :

- `GET /2/users/{id}/tweets` : `max_results` (5 à 100), `since_id`, `until_id`,
  `pagination_token`, `exclude` (`replies`, `retweets`), `start_time`, `end_time` ;
  réponse `data`, `includes` (`media`, `posts`, `users`…), `meta` (`newest_id`,
  `oldest_id`, `next_token`, `result_count`).
- `GET /2/users/by/username/{username}`, `GET /2/users/{id}` : `user.fields`.
- `GET /2/tweets/{id}`, `GET /2/tweets?ids=` : lecture d'un ou plusieurs posts (contexte
  parent et cité en un seul appel).
- **Noms actuels des paramètres** : `post.fields` (et non plus `tweet.fields`),
  expansion `referenced_posts`, champ `note_post` (texte long), `includes.posts`.
  L'adaptateur lit aussi les anciens noms (`referenced_tweets`, `note_tweet`,
  `includes.tweets`) par prudence. Demandé : `author_id`, `conversation_id`,
  `created_at`, `lang`, `public_metrics`, `referenced_posts`, `attachments`,
  `note_post`, `entities` ; médias : `type`, `url`, `preview_image_url`. Pas
  d'expansion des posts référencés pendant le polling (chaque post en `includes` est
  facturé) : le contexte se lit à la demande (`get_thread_context`).
- En-têtes de débit : `x-rate-limit-limit`, `x-rate-limit-remaining`,
  `x-rate-limit-reset` (epoch Unix) ; 429 jusqu'à la réinitialisation. Limites par
  application et par 15 min : `/2/users/:id/tweets` 10 000, `/2/tweets` 3 500,
  `/2/tweets/:id` 450, utilisateurs 300.

**Non vérifié** : aucun appel réel (pas de clé). Le nom `post.fields` vient de la
spécification actuelle ; si l'API réelle le refusait, ne changer que les constantes de
`x_client.py`.

## Configuration

Voir [configuration.md](configuration.md#radar-service-hangar-radar). Activer :
`TOWER_SERVICES=ping,ingest,radar` et `TOWER_X_BEARER_TOKEN`.
