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

## Modèles (LLM)

Voir [llm.md](llm.md). Les clés sont des secrets : lues par la configuration
(`SecretStr`), jamais journalisées ni mises dans un contexte d'erreur.

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_OPENAI_API_KEY` | vide | Organisation OpenAI **sans** partage des données (routes `data_sharing_ok = false`) |
| `TOWER_OPENAI_API_KEY_SHARED` | vide | Organisation OpenAI **avec** partage (allocation gratuite ; données publiques seulement) |
| `TOWER_ANTHROPIC_API_KEY` | vide | Clé Anthropic |
| `TOWER_OPENAI_USAGE_TIER` | `1` | Palier d'usage OpenAI (1 à 5) : fixe l'allocation gratuite quotidienne |
| `TOWER_LLM_FREE_MARGIN` | `0.10` | Marge appliquée à l'estimation avant d'utiliser l'allocation gratuite |

## Ingestion et fact-check

Voir [ingestion.md](ingestion.md).

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_FC_LANGUAGES` | `fr,en` | Langues traitées par le fact-check (sous-étiquettes BCP 47) |
| `TOWER_INGEST_MAX_ITEMS` | `50` | Items au plus par lot d'ingestion |
| `TOWER_INGEST_DAILY_QUOTA` | `500` | Items acceptés par installation et par jour UTC |
| `TOWER_HTTP_USER_AGENT` | `Seat12A-Tower/0.1` | User-Agent des récupérations web ; y ajouter une URL de contact générique, jamais une adresse personnelle |

### Pipeline de fact-check (services `factcheck` et `investigate`)

Voir [fact-check.md](fact-check.md#réglages).

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_LLM_FAKE` | `false` | Fournisseur de modèles factice (développement, smoke test, démonstration de l'évaluation). **Refusé** quand `TOWER_ENV=prod` (`TWR-FCK-016`) |
| `TOWER_FC_DRAFT_DAILY_QUOTA` | `50` | Brouillons envoyés en relecture par jour UTC ; le surplus attend une place |
| `TOWER_FC_CAP_STANDARD_USD` | `0.10` | Plafond de coût d'une enquête standard : au-delà, pas d'escalade |
| `TOWER_FC_CAP_REINFORCED_USD` | `0.40` | Plafond d'une enquête renforcée (N3) |
| `TOWER_FC_CAP_N4_USD` | `0.60` | Plafond d'une enquête N4 |
| `TOWER_FC_SEARCHES_STANDARD` | `4` | Recherches web d'une enquête standard |
| `TOWER_FC_SEARCHES_REINFORCED_EXTRA` | `4` | Recherches web supplémentaires en profondeur renforcée |
| `TOWER_FC_PASSAGES_STANDARD` | `30` | Passages donnés au juge (environ 160 tokens chacun) |
| `TOWER_FC_PASSAGES_REINFORCED` | `60` | Idem en profondeur renforcée |
| `TOWER_FC_PASSAGE_WORDS` | `120` | Taille cible d'un passage (mots) |
| `TOWER_FC_CONFIDENCE_COEFFICIENTS` | vide | Objet JSON qui remplace des coefficients de la confiance (noms de `Coefficients` dans `core/domain/factcheck/confidence.py`) |
| `TOWER_FC_CONFIDENCE_CAP` | `0.90` | Plafond de la confiance avant calibration |
| `TOWER_FC_CONFIDENCE_HIGH` | `0.80` | Seuil de la bande haute |
| `TOWER_FC_CONFIDENCE_MEDIUM` | `0.55` | Seuil de la bande moyenne |
| `TOWER_FC_ESCALATION_THRESHOLD` | `0.60` | Confiance provisoire sous laquelle une enquête passe en profondeur renforcée |
| `TOWER_FC_LOADED_TERMS_FILE` | vide | Lexique chargé (JSON, par langue) ; vide : `core/factcheck/loaded_terms.json` |
| `TOWER_FC_REFERENCE_LANGS` | `fr` | Langues de référence toujours rédigées en plus de la langue du post (l'anglais est ajouté pour un post en anglais) |
| `TOWER_FC_REVIEW_LOCALE_FALLBACK` | `fr` | Langue de relecture par défaut |

### Fact-check V1 (lot 4)

Voir [fact-check.md](fact-check.md#réglages-du-lot-4). Les réglages par niveau marqués
« profil » ne servent qu'au **profil par défaut** (version 0) : dès qu'un profil est
activé en base (`processing_profiles`), ce sont ses valeurs qui comptent.

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_FC_EMBED_BATCH_SIZE` | `2000` | Contenus par job `factcheck.embed_batch` (100 à 2 000 ; requêtes de 256 textes) |
| `TOWER_FC_EMBED_DEBOUNCE_SECONDS` | `30` | Délai avant le lot : les contenus d'une même minute partagent un lot |
| `TOWER_FC_EMBEDDING_RETENTION_DAYS` | `14` | Conservation des vecteurs de posts (partitions hebdomadaires) |
| `TOWER_FC_E1_SAMPLE_RATE` | `0.02` | Profil : part des contenus écartés gardés au hasard (faux négatifs) |
| `TOWER_FC_E1_THRESHOLDS` | `{"N1": 0.15, "N2": 0.15, "N3": 0.05, "N4": 0.0}` | Profil : seuils du classifieur E1 par niveau |
| `TOWER_FC_MATCH_CANDIDATES` | `5` | Groupes les plus proches comparés par E3 |
| `TOWER_FC_MATCH_SAME_THRESHOLD` | `0.92` | Profil : similarité au-delà de laquelle une paire de même langue à signature identique est rattachée sans arbitre (borne ≥ 0,85) |
| `TOWER_FC_MATCH_GREY_THRESHOLD` | `0.80` | Profil : bas de la zone grise (arbitre) ; en dessous, paire différente |
| `TOWER_FC_CURRENT_PERIOD_DAYS` | `31` | Sans fiche, deux affirmations « actuellement » partagent une période si leurs posts sont à moins de N jours |
| `TOWER_FC_BAND_TAU_RATIO` | `0.05` | Tolérance des bandes : τ = max(plancher, ratio × référence) |
| `TOWER_FC_BAND_FLOOR_RATE_POINTS` | `0.2` | Plancher de τ pour un taux (en points) |
| `TOWER_FC_BAND_FLOOR_DEFAULT` | `0` | Plancher de τ pour les autres unités |
| `TOWER_FC_VERDICT_TTL_DAYS` | vide | Objet JSON qui remplace la validité d'un verdict par type d'affirmation, en jours (défauts dans [fact-check.md](fact-check.md#péremption-et-revalidation)) |
| `TOWER_FC_LEVEL_BUDGETS_USD` | `{"N0": 1, "N1": 5, "N2": 10, "N3": 20, "N4": 15}` | Profil : plafonds mensuels des portées `level:N0..N4` (créées par `tower llm seed`) |
| `TOWER_FC_INSEE_INDICATORS` | vide | Liste JSON des indicateurs INSEE suivis, versés automatiquement en fiches à valider |
| `TOWER_FC_EXTERNAL_FACTCHECK_LOOKUP` | `true` | E3 cherche les fact-checks existants (Google Fact Check Tools) pour un nouveau groupe |

### Fact-check V2 (lot 5)

Voir [fact-check.md](fact-check.md#lot-5--v2--flux-de-masse-ordonnanceur-récits-et-vagues).

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_FC_MASS_FLOW` | `true` | Flux de masse N1 : extraction en micro-lots (E2) et groupes dormants confiés à l'ordonnanceur. `false` : le composeur ne fait rien |
| `TOWER_FC_BATCH_SIZE` | `10` | Posts par micro-lot (1 à 12) |
| `TOWER_FC_BATCH_MAX_TOKENS` | `2500` | Taille maximale estimée d'un lot (un post plus long part seul) |
| `TOWER_FC_BATCH_MAX_WAIT_S` | `21600` | Attente maximale du plus ancien post avant l'envoi d'un lot incomplet |
| `TOWER_FC_BATCH_DISSIMILARITY` | `0.6` | Cosinus au-delà duquel deux posts ne partagent pas un lot |
| `TOWER_FC_CANARY_RATE` | `0.01` | Part des lots qui reçoivent un post canari (détection de contamination) |
| `TOWER_FC_SOLO_AUDIT_RATE` | `0.02` | Probabilité, par position, de rejouer un post seul pour audit |
| `TOWER_FC_SCHEDULER_TARGET_PER_PASS` | `5` | Nouveaux contenus visés par passe : intervalle = 3600 × cible ÷ débit horaire |
| `TOWER_FC_SCHEDULER_MIN_INTERVAL_S` | `60` | Intervalle minimal entre deux passes |
| `TOWER_FC_SCHEDULER_MAX_INTERVAL_S` | `1800` | Intervalle maximal (flux calme) |
| `TOWER_FC_PROMOTION_OBSERVERS` | `5` | Promotion N1 → N2 : observateurs distincts sur 6 h |
| `TOWER_FC_PROMOTION_VIEWS` | `50000` | Promotion N1 → N2 : vues au-delà de ce seuil |
| `TOWER_FC_PROMOTION_VELOCITY_FACTOR` | `3` | Promotion N1 → N2 : observations de la dernière heure ÷ heure précédente (au moins 10) |
| `TOWER_FC_DEMOTION_HOURS` | `24` | Rétrogradation N2 → N1 après N heures sous toutes les conditions |
| `TOWER_FC_LIGHT_CHECK_SHARE` | `0.2` | Part des nouveaux groupes N1 (les plus prioritaires) qui reçoivent une vérification légère |
| `TOWER_FC_DEEP_QUEUE_CAPACITY` | `50` | Jobs en attente ou en cours dans `fc-deep` au-delà desquels l'ordonnanceur retient les enquêtes N2 (`TWR-FCK-045`) |
| `TOWER_FC_NARRATIVE_SEMANTIC_THRESHOLD` | `0.80` | Signal sémantique : cosinus à un médoïde du récit |
| `TOWER_FC_NARRATIVE_MEDOID_FLOOR` | `0.60` | Plancher : cosinus minimal à au moins un médoïde pour rattacher |
| `TOWER_FC_NARRATIVE_RARE_ENTITY_MAX` | `50` | Entité « rare » (ancre) : au plus N affirmations sur 30 jours |
| `TOWER_FC_NARRATIVE_CLOSE_DAYS` | `7` | Fermeture d'un récit inactif depuis N jours |
| `TOWER_FC_NARRATIVE_SPLIT_GROUPS` | `25` | Scission suggérée au-delà de N groupes |
| `TOWER_FC_WAVE_THRESHOLDS` | voir description | JSON des paliers sur la fenêtre d'une heure : `{"watch": {"share": 0.01, "accounts": 10}, "surge": {"share": 0.03, "accounts": 30, "growth": 2.0}, "alert": {"share": 0.08, "accounts": 80}}` |
| `TOWER_FC_WATCHED_THEMES` | `health,elections,security,disaster` | Thèmes sensibles : un récit de ces thèmes au palier `watch` passe en `alert` |
| `TOWER_FC_SURGE_DAILY_QUOTA` | `3` | Quota quotidien séparé des brouillons en mode vague |
| `TOWER_FC_SURGE_END_HOURS` | `6` | Fin de vague après N heures sous le palier `watch` |
| `TOWER_FC_SURGE_VERDICT_TTL_HOURS` | `12` | Validité maximale d'un verdict validé pendant une vague |
| `TOWER_FC_SAMPLING_PER_MINUTE` | `200` | Au-delà de N posts par minute proches d'un récit en vague, seul un échantillon stratifié est extrait |
| `TOWER_FC_NOVELTY_POSTS` | `3` | Empreinte de nouveauté retenue à partir de N posts écartés… |
| `TOWER_FC_NOVELTY_ACCOUNTS` | `3` | … venant d'au moins N comptes |
| `TOWER_FC_NOVELTY_BUDGET_SHARE` | `0.2` | Part maximale des extractions horaires d'un récit forcées par la nouveauté |

### Sources de connaissance (Atlas)

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_GOOGLE_API_KEY` | — | Clé Google Fact Check Tools (`claims:search`) ; absente : source sautée |
| `TOWER_BRAVE_API_KEY` | — | Jeton Brave Search API ; absent : pas de recherche web |
| `TOWER_PISTE_CLIENT_ID` / `TOWER_PISTE_CLIENT_SECRET` | — | Légifrance via PISTE (OAuth2, identifiants client) ; absents : source sautée |
| `TOWER_PISTE_SANDBOX` | `false` | Utiliser les points d'accès bac à sable de PISTE |
| `TOWER_INSEE_API_KEY` | — | Clé du portail des API de l'INSEE, si le portail l'exige |
| `TOWER_INSEE_SERIES` | vide | Objet JSON `{"indicateur": "idbank"}` des séries BDM consultées ; vide : source sautée |
| `TOWER_ATLAS_WIKIPEDIA_CONCURRENCY` | `3` | Requêtes Wikimedia simultanées par processus (limite Wikimedia : 3) |
| `TOWER_ATLAS_CACHE_TTL_S` | `604800` | Fraîcheur par défaut de `source_cache` (7 jours ; résultats de recherche : 1 jour) |
| `TOWER_ATLAS_HTTP_TIMEOUT_S` | `10` | Délai des appels aux sources |

## Budgets

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_BUDGET_RESERVATION_TTL_S` | `1800` | Une réservation de budget (ou d'allocation gratuite) non réglée après ce délai est libérée par la maintenance (voir [llm.md](llm.md#budgets)) |

## Radar (service hangar `radar`)

Voir [radar.md](radar.md). Le jeton est un secret (`SecretStr`), jamais journalisé.

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_X_BEARER_TOKEN` | vide | Jeton d'application (bearer) de l'API X ; absent : `TWR-RAD-007` |
| `TOWER_X_API_BASE` | `https://api.x.com` | URL de l'API X (faux serveur local pour les essais) |
| `TOWER_X_PRICE_POST_READ_USD` | `0.005` | Prix d'une lecture de post |
| `TOWER_X_PRICE_USER_READ_USD` | `0.010` | Prix d'une lecture d'utilisateur |
| `TOWER_X_MONTHLY_CAP_USD` | `20` | Plafond initial de la portée `provider:x` (créé par `tower llm seed` ou par le radar s'il manque), modifiable ensuite par `tower llm budget provider:x <usd>` |
| `TOWER_RADAR_MAX_RESULTS` | `20` | Posts demandés par page (bornes X : 5 à 100) |
| `TOWER_RADAR_MAX_PAGES` | `3` | Pages lues au plus par lecture d'un compte |
| `TOWER_RADAR_EXCLUDE` | `retweets` | Paramètre `exclude` : `retweets`, `replies`, les deux ou vide |
| `TOWER_RADAR_INTERVAL_HIGH_S` | `120` | Intervalle de base, priorité ≥ 50 |
| `TOWER_RADAR_INTERVAL_NORMAL_S` | `600` | Intervalle de base, priorité 0 à 49 |
| `TOWER_RADAR_INTERVAL_LOW_S` | `1800` | Intervalle de base, priorité < 0 |
| `TOWER_RADAR_INTERVAL_MIN_S` | `60` | Intervalle minimal |
| `TOWER_RADAR_INTERVAL_MAX_S` | `21600` | Intervalle maximal (6 h) |
| `TOWER_RADAR_EXPECTED_POSTS_PER_POLL` | `2` | Posts renvoyés en moyenne par lecture (une lecture vide ne coûte rien) : sert au plafond quotidien de lectures |
| `TOWER_RADAR_SCHEDULE_BATCH` | `100` | Lectures enfilées au plus par passage de la planification |

## intercom (bot Discord)

Voir [intercom.md](intercom.md). Le token Discord et le token de gate sont des secrets.

| Variable | Défaut | Description |
|---|---|---|
| `TOWER_DISCORD_TOKEN` | vide | Token du bot Discord (obligatoire hors `--dry-run`) |
| `TOWER_DISCORD_GUILD_ID` | vide | Serveur où synchroniser les commandes (immédiat) ; vide : synchronisation globale |
| `TOWER_DISCORD_CHANNEL_INCIDENTS` | vide | Salon des incidents (`incident.opened`, `incident.reopened`) |
| `TOWER_DISCORD_CHANNEL_BUDGET` | vide | Salon du budget (`budget.threshold_reached`) |
| `TOWER_DISCORD_CHANNEL_DOSSIERS` | vide | Salon des dossiers (`dossier.opened`, `dossier.transitioned`) |
| `TOWER_DISCORD_CHANNEL_ADMIN` | vide | Salon d'administration (`watchlist.changed`, `extension_filters.published`) |
| `TOWER_DISCORD_LOCALE` | `fr` | Langue des notifications dans les salons (`fr` ou `en`) ; les réponses aux commandes suivent la langue Discord de l'utilisateur |
| `TOWER_GATE_URL` | `http://localhost:8000` | URL de gate (réseau privé Railway en production) |
| `TOWER_GATE_TOKEN` | vide | Token de service du bot |
| `TOWER_INTERCOM_CURSOR_NAME` | `intercom` | Nom du curseur SSE stocké côté gate |

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
