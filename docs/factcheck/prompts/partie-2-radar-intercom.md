# Prompt d'implémentation — Partie 2 : radar X et bot Discord

Prompt à donner tel quel à une nouvelle session Claude Code (Opus 5.5). Il ne suppose
aucune connaissance préalable du projet. Rédigé le 2026-11-11, après la livraison de
la partie 1 (PR #4 fusionnée dans `main`).

---

Tu es l'agent principal (Claude Opus 5.5) chargé d'implémenter la **partie 2** du
backend « 12A Tower ». Tu orchestres des sous-agents **Claude Sonnet 5.5** et tu
restes responsable de la qualité, de l'intégration et des vérifications finales. Tu
ne connais pas encore le projet : la section 1 t'en donne l'essentiel, la
documentation du dépôt fait foi.

## 1. Le projet en bref

**Seat 12A** est un service de fact-checking et de contexte sur les réseaux sociaux.
Il est francophone, mais couvre tous les pays et toutes les langues. Son écosystème
comprend un site public, une extension Chromium et un bot Discord interne pour
l'équipe. Le développeur principal est Jules.

**12A Tower** (ce dépôt) est le backend : Python 3.13 asyncio, un seul codebase, une
seule image Docker, plusieurs processus déployés sur Railway.

| Processus | Rôle |
|---|---|
| `tower gate` | API FastAPI unique (site, extension, bot), flux d'événements SSE |
| `tower hangar` | Worker générique : jobs, abonnements et tâches périodiques des services activés (`TOWER_SERVICES`) |
| `tower migrate` | Migrations Alembic (pre-deploy) |

**PostgreSQL est la seule infrastructure** : données, outbox d'événements avec
LISTEN/NOTIFY, file de jobs en `SKIP LOCKED`, verrous consultatifs, erreurs.

Principes structurants :
- **RAM** : Railway facture environ 10 $ par Go et par mois, donc imports paresseux,
  aucun modèle local, caches bornés, un processus asyncio par réplica.
- **Coût minimal** sur tout.
- **Scalabilité** par réplicas sans état.
- **Langues** : chaque contenu porte sa langue ; ce que lit l'équipe existe en
  français et en anglais.
- **Multi-plateforme** : X d'abord, mais chaque donnée porte sa plateforme.
- **Objectivité et égalité de traitement**.
- **Aucune publication automatique sur X, aucun scraping.**

### État actuel (fait)

- **Socle** (`docs/sessions/2026-11-10-socle-tower.md`) :
  - config, logs JSON, base, migrations ;
  - dossiers (cycle `received → in_review → validated/rejected → published`) ;
  - outbox et événements SSE (`GET /v1/events`, reprise par `Last-Event-ID` ou
    `?after=`) ;
  - file de jobs (`RetryLater`, `PermanentError`, idempotence) ;
  - services enfichables (`Service` : jobs, abonnements, tâches périodiques
    `Service.periodic`) ;
  - auth : tokens de service, staff avec `X-Acting-Discord-Id`, rôles `reviewer`,
    `editor`, `admin`, permissions vérifiées dans les cas d'usage ;
  - erreurs à codes stables `TWR-*`.
- **Partie 1** (`docs/sessions/2026-11-11-partie-1-fondations-ingestion.md`,
  382 tests verts) :
  - routeur de modèles OpenAI et Anthropic (`docs/llm.md`) : routes versionnées par
    rôle, cache de contenu, allocation gratuite OpenAI, budgets réservés
    atomiquement dans `budget_counters`, `tower llm seed` et `tower llm usage` ;
  - ports et adaptateurs (`core/ports`, `core/adapters`, dont `PlatformClient` et un
    `http_fetcher` protégé contre le SSRF) ;
  - ingestion générique (`docs/ingestion.md`) : `content_items`,
    `platform_accounts`, `watched_accounts` et son historique,
    `POST /v1/ingest/observations`, configuration de l'extension, service `ingest`
    (liaison des copies par SimHash, partitions des observations, secrets
    d'installation quotidiens).

Le **plan complet du fact-check** est dans `docs/factcheck/` (sommaire :
`docs/factcheck/README.md`, feuille de route : `docs/factcheck/feuille-de-route.md`).
La partie 2 prépare le lot 3 (MVP sur les comptes suivis) : il faut lire les posts
des comptes suivis sur X, et donner à l'équipe son outil de travail, le bot Discord.

## 2. Lire avant toute action

1. `CLAUDE.md` : règles non négociables (langue : code en anglais, documentation en
   français ; architecture ; RAM ; erreurs ; logs ; définition de « terminé »). Elles
   priment sur ce prompt.
2. `docs/sessions/2026-11-11-partie-1-fondations-ingestion.md` (état et points
   ouverts), puis `docs/sessions/2026-11-10-socle-tower.md`.
3. `docs/architecture.md`, `docs/services.md`, `docs/events.md`, `docs/jobs.md`,
   `docs/auth.md`, `docs/api.md`, `docs/llm.md`, `docs/ingestion.md`,
   `docs/configuration.md`, `docs/decisions.md`.
4. Du plan, seulement : `docs/factcheck/plateformes-medias.md` (port
   `PlatformClient`, X), `docs/factcheck/niveaux.md` (comptes suivis),
   `docs/factcheck/contexte.md` (section « Le brouillon dans Discord »),
   `docs/factcheck/international.md` (section « Langues de publication »).
5. Le code utile : `src/tower/core/ports/platforms.py`, `core/usecases/ingestion.py`,
   `core/usecases/watchlist.py`, `core/models/{ingestion,llm}.py`,
   `core/llm/router.py`, `core/services/`, `services/ingest/`, `gate/routes/`,
   `cli.py`, `tests/conftest.py`.

Ne refais pas les recherches déjà documentées. Si un point du plan te paraît faux,
note-le et pose la question à Jules au lieu de le changer en silence.

## 3. Périmètre

### Inclus

**A. Radar X, en service hangar `radar`** (et non en processus dédié). Raison : la
RAM. Un processus de plus coûte environ 70 Mo ; un service hangar ajoute quelques
Mo, et peut tourner sur un réplica dédié si besoin (`TOWER_SERVICES`). Consigne cette
décision dans `docs/decisions.md` et mets à jour `docs/architecture.md` et `CLAUDE.md`
(le tableau mentionne `tower radar`).
- Adaptateur `XPlatformClient` (`core/adapters/x_client.py`) qui implémente
  `PlatformClient` pour l'API officielle X v2, en httpx importé paresseusement.
- Résolution des comptes suivis (pseudo → identifiant), lecture des nouveaux posts par
  compte avec `since_id`, conversion en items d'ingestion, appel du cas d'usage
  d'ingestion existant (source `radar`).
- Planification adaptative et **plafonnée par le budget X** (détail en 4.1).
- Suivi du coût de chaque lecture dans le budget (portée `provider:x`).

**B. Bot Discord `tower intercom`** (nouveau processus, client de gate uniquement).
- Notifications des événements.
- Commandes pour la liste de suivi, les dossiers, les incidents et l'usage.
- Interface en français et en anglais selon la langue Discord de l'utilisateur.
- Reprise du flux SSE sans perte grâce à un curseur stocké côté gate.

**C. Points ouverts de la partie 1 et besoins du bot**
- Libération des **réservations de budget orphelines** : un processus qui meurt
  entre réservation et règlement bloque aujourd'hui du budget.
- Endpoint `GET /v1/llm/usage` (staff) pour le bot.
- Endpoints de curseur SSE par client de service.

### Exclu (parties suivantes)

- Pipeline de fact-check : extraction, cadrage, collecte, jugement, brouillons. Ils
  arrivent au lot 3 et demandent les clés d'API et le jeu de référence.
- Revérification d'un post par l'API (lot 3) : l'adaptateur expose `get_item`, mais
  aucun job ne l'utilise encore.
- Règles d'ouverture de dossier automatiques.
- Autres plateformes que X.
- Empreinte des images.
- Changement du seuil SimHash : question ouverte pour Jules, à trancher sur un corpus
  réel.

**Aucune clé n'est disponible** (ni X, ni Discord) : aucun appel réseau réel. Tout
est testé avec des doublures (`httpx.MockTransport`, faux gate, application gate
réelle via `httpx.ASGITransport`).

## 4. Spécifications

### 4.1 Radar X

**Avant d'écrire l'adaptateur**, vérifie dans la documentation officielle de X
(docs.x.com) : les endpoints, les champs, les limites de débit et la tarification au
paiement à l'usage. Notre documentation indique, d'après des sources secondaires,
environ 0,005 $ par lecture de post et 0,010 $ par lecture d'utilisateur, avec un
plafond mensuel de lectures de 2 ou 3 millions. Les noms exacts des paramètres
doivent venir de la documentation officielle, pas de ta mémoire. Consigne ce que tu
as vérifié.

**Adaptateur** `XPlatformClient(bearer_token, http=None)` :
- `get_account(handle_or_id)` : lecture d'utilisateur par pseudo ou par identifiant ;
- `poll_account(account_ref, since=since_id)` : posts récents, avec `max_results`
  borné ;
- `get_item(external_id)`, `get_metrics(external_id)`,
  `get_thread_context(item)` : posts parent et cité, une seule lecture groupée si
  l'API le permet.
- Champs à demander : date de création, langue, métriques publiques, posts
  référencés, médias, texte long des posts étendus le cas échéant.
- Conversion en `ContentItem` du port, identifiants en chaînes.
- Erreurs : 429 → `RetryLater` jusqu'à la réinitialisation indiquée par l'en-tête ;
  401 ou 403 → erreur fatale typée, stockée ; 404 → `None` ; 5xx → relançable.
- Codes d'erreur `TWR-RAD-*` (nouveau domaine).
- Variable : `TOWER_X_BEARER_TOKEN`, secret jamais journalisé.

**Coût et budget** :
- Chaque appel réserve son coût estimé dans `budget_counters`, portée
  `provider:x` (nombre maximal de posts × prix unitaire), puis règle le coût réel
  (posts réellement renvoyés). Réutilise le mécanisme existant du routeur ; si besoin,
  extrais-le dans un module commun plutôt que de le dupliquer.
- Prix unitaires en configuration (`TOWER_X_PRICE_POST_READ_USD`, défaut 0.005 ;
  `TOWER_X_PRICE_USER_READ_USD`, défaut 0.010) et plafond mensuel en base (portée
  `provider:x`, défaut 20 $, ajusté par `tower llm seed` ou une commande dédiée).
- Budget épuisé : la lecture attend la période suivante (`RetryLater`), et
  `budget.threshold_reached` est publié comme pour les modèles.

**Planification adaptative** (pas de cadence fixe ; Jules veut un système qui
s'adapte au volume) :
- Ajouter à `watched_accounts` (migration expand) : `next_poll_at`,
  `last_polled_at`, `poll_interval_s`, `last_post_at`, `consecutive_empty`.
- Tâche périodique singleton `radar.schedule` (toutes les 60 s) : sélectionne les
  comptes X actifs dont `next_poll_at` est dépassé, par priorité, dans la limite des
  lectures autorisées aujourd'hui. Ce plafond vaut budget restant du mois ÷ jours
  restants ÷ coût moyen d'une lecture. La tâche enfile `radar.poll_account` (file
  `radar`, `dedupe_key` par compte).
- Intervalle par compte : base selon la priorité (par exemple 2 min, 10 min,
  30 min), divisé quand le compte publie souvent, multiplié par 2 à chaque lecture
  vide jusqu'à un plafond (par exemple 6 h). Bornes en configuration.
- `radar.poll_account` : lit avec `since_id`, ingère par le cas d'usage d'ingestion
  (une observation par post, source `radar`), met à jour `since`, `next_poll_at`, les
  compteurs. Idempotent : rejouer le même job ne crée pas de doublon.
- `radar.resolve_account` : un compte ajouté par pseudo sans identifiant est résolu
  (lecture d'utilisateur), en job séparé, déclenché par l'événement
  `watchlist.changed`. L'abonnement reste court : il enfile seulement.

**Tests** :
- conversion d'une réponse X réaliste en items ;
- pagination et `since_id` ;
- 429 avec reprise différée ;
- erreurs 401 et 404 ;
- réservation et règlement du coût ;
- budget épuisé → `RetryLater` ;
- planification (priorité, recul exponentiel, plafond quotidien) ;
- idempotence ;
- isolation des imports (hangar sans `radar` ne charge pas l'adaptateur).

### 4.2 Bot Discord `tower intercom`

**Architecture** :
- `src/tower/intercom/` : `app.py` (client discord.py, importé paresseusement par la
  CLI) ; `gate_client.py` (client HTTP de gate : token de service et
  `X-Acting-Discord-Id` de la personne qui agit) ; `handlers/` (logique métier pure,
  indépendante de discord.py, testable sans Discord) ; `i18n/` (catalogues `fr` et
  `en`) ; `notifier.py` (événements SSE → messages).
- **Le bot n'accède jamais à la base**. Il passe par gate, comme tous les clients.
- `tower intercom` dans la CLI, `railway/intercom.json` sur le modèle des fichiers
  existants, Dockerfile inchangé si possible.

**RAM** (cible : au plus 120 Mo de RSS au repos, à mesurer) :
- intents minimaux (serveurs et interactions ; **pas** de contenu des messages) ;
- `member_cache_flags` à aucun ;
- `chunk_guilds_at_startup=False` ;
- `max_messages=None` (pas de cache de messages) ;
- une seule connexion SSE ;
- caches bornés.

**Événements notifiés** (salons configurables) :
- `incident.opened` et `incident.reopened` → salon incidents ;
- `budget.threshold_reached` → salon budget ;
- `dossier.opened` et `dossier.transitioned` → salon dossiers ;
- `watchlist.changed` et `extension_filters.published` → salon administration.

Messages courts, avec la plateforme d'origine affichée. Aucun événement à haut volume
(`content.ingested`) : le bot ne s'y abonne pas.

**Reprise du flux sans perte** :
- nouveaux endpoints gate `GET /v1/events/cursors/{name}` et
  `PUT /v1/events/cursors/{name}` (permission `events:read`, un curseur par client de
  service, stocké en base) ;
- le bot enregistre son curseur après chaque lot traité et reprend avec `?after=` au
  redémarrage ;
- livraison au moins une fois : les notifications sont idempotentes côté bot
  (identifiant d'événement mémorisé dans un ensemble borné).

**Commandes slash** (vérification des permissions par gate, jamais par le bot seul) :
- `/watchlist list|add|edit|deactivate` : plateforme, pseudo, catégorie de rôle
  public, priorité, via les endpoints existants. L'ajout par pseudo déclenche la
  résolution par le radar.
- `/dossier show|take|validate|reject|publish` : endpoints de dossiers existants ;
  `publish` demande le lien du post publié, et la publication sur X reste manuelle.
- `/incidents list|resolve` : endpoints d'erreurs existants.
- `/usage` : nouvel endpoint `GET /v1/llm/usage` (permission à créer,
  `usage:read`, pour `editor` et `admin`) : allocation gratuite du jour, dépense du
  mois par portée (`global`, fournisseurs, `provider:x`).
- Réponses éphémères pour les erreurs ; les codes `TWR-*` sont affichés avec un
  message traduit.

**Langues** : chaque texte affiché vient du catalogue `fr` ou `en` choisi d'après la
langue Discord de l'utilisateur (`interaction.locale`), avec l'anglais par défaut.
Les clés de catalogue et le code sont en anglais. Un test vérifie que les deux
catalogues ont exactement les mêmes clés.

**Variables** : `TOWER_DISCORD_TOKEN`, `TOWER_DISCORD_GUILD_ID`,
`TOWER_DISCORD_CHANNEL_INCIDENTS`, `_BUDGET`, `_DOSSIERS`, `_ADMIN`,
`TOWER_GATE_URL`, `TOWER_GATE_TOKEN`. Secrets jamais journalisés.

**Tests** :
- les gestionnaires, avec un faux gate et avec la vraie application gate via
  `httpx.ASGITransport` sur la vraie base ;
- le notificateur : idempotence, reprise du curseur ;
- la parité des catalogues ;
- l'isolation des imports : gate et hangar ne chargent jamais `discord`.

Aucune connexion réelle à Discord dans les tests.

### 4.3 Réservations orphelines et usage

- **Réservations** : table `budget_reservations` (migration expand) : portée,
  montant, créée le, expire le, référence de l'appel. Le routeur (et le radar)
  insèrent une réservation en même temps que l'incrément de `reserved` ; le règlement
  la supprime ; une tâche périodique singleton libère les réservations expirées
  (décrément de `reserved`, suppression, journalisation). Même principe pour
  l'allocation gratuite si elle est réservée. Les tests couvrent les cas suivants : un
  processus tué entre réservation et règlement voit sa réservation libérée après
  expiration ; aucun double décrément.
- **`GET /v1/llm/usage`** : réutilise la logique de `tower llm usage`, en un seul
  endroit.

### 4.4 Codes, configuration, événements, documentation

- Codes `TWR-RAD-*`, `TWR-BOT-*` si nécessaire, puis régénérer `docs/errors.md`.
- Variables dans `docs/configuration.md` et `.env.example`.
- Topics éventuels dans `docs/events.md`.
- Endpoints dans `docs/api.md`, permission `usage:read` dans `docs/auth.md`.
- Nouvelles pages `docs/radar.md` et `docs/intercom.md` ; mises à jour de
  `architecture.md`, `services.md`, `deployment-railway.md` (service intercom, radar
  dans hangar), `roadmap.md`, `docs/factcheck/feuille-de-route.md`, `docs/README.md`,
  `docs/decisions.md`.
- Résumé de session : `docs/sessions/AAAA-MM-JJ-partie-2-radar-intercom.md`.

## 5. Exigences transversales

- **RAM** :
  - imports paresseux de `httpx` (adaptateur X) et `discord` ;
  - test d'isolation des imports ;
  - mesure du RSS au repos de gate, de hangar (`ping,ingest`, puis
    `ping,ingest,radar`) et d'intercom, avant et après. Références de la partie 1 :
    gate 83-84 Mo, hangar 68 Mo. Cibles : gate au plus +5 Mo, hangar avec `radar` au
    plus +10 Mo, intercom au plus 120 Mo ;
  - aucun numpy, pandas ni lxml.
- **Scalabilité** : aucun état en mémoire hors caches bornés ; planification et
  budgets en base ; jobs idempotents ; un seul réplica intercom, mais rien ne casse si
  deux tournent (notifications idempotentes, curseur en base).
- **Langues** : catalogues `fr` et `en` du bot ; contenus ingérés avec leur langue.
- **Multi-plateforme** : rien de spécifique à X hors de l'adaptateur et du job de
  lecture ; la planification lit `platform` pour être réutilisable plus tard.
- **Socle** :
  - toute écriture métier passe par un cas d'usage (permission, audit, événement dans
    la même transaction) ;
  - services n'important que `tower.core` ;
  - abonnements courts ;
  - statuts en `TEXT` + `CHECK` ;
  - IDs X en `BIGINT` côté base pour les requêtes numériques, en chaînes dans l'API ;
  - migrations expand/contract.
- **Code en anglais, documentation en français.**

## 6. Méthode de travail

**Branche** : pars de `main` et travaille sur la branche que ta session t'indique. Pas
de pull request sans demande explicite de Jules.

**Base de test** : PostgreSQL 16 réel (voir `docs/development.md` ; défaut des tests
`postgresql://postgres@localhost:5433/tower_test`). Installe-le s'il manque, comme à
la partie 1. Ne simule jamais la base.

**Phase A — Contrats (toi seul)**
1. Vérifie que les 382 tests passent.
2. Écris :
   - les contrats : signatures de l'adaptateur X, du client gate du bot et des
     gestionnaires ;
   - les modèles SQLAlchemy et **une** migration `0004` (colonnes de planification,
     `budget_reservations`, curseurs SSE si une table est nécessaire) ;
   - les codes d'erreur, les variables et la permission `usage:read` ;
   - les points d'entrée CLI.
3. Vérifie `alembic check`, `ruff` et `mypy`, puis commit.

**Phase B — Sous-agents Claude Sonnet 5.5 en parallèle**

Lance-les avec le modèle **Claude Sonnet 5.5** (`claude-sonnet-5-5` ; dans l'outil
Agent, `model: "sonnet"`). Vérifie au premier lancement que c'est bien Sonnet 5.5.

Chaque brief est **autonome** (ils ne voient pas cette conversation) et contient :
- les fichiers à créer ou modifier, et seulement ceux-là ;
- les signatures exactes ;
- le comportement et les cas limites ;
- les tests à écrire ;
- les commandes (`uv run ruff format`, `uv run ruff check`, `uv run mypy`,
  `uv run pytest` sur ses tests) ;
- les interdits : aucun commit, aucun fichier hors liste, aucun import lourd au
  niveau module, aucun réseau ;
- un rapport de 10 lignes au plus.

| Agent | Contenu | Dépend de |
|---|---|---|
| R1 | Adaptateur `XPlatformClient` + tests (`MockTransport`) | contrats |
| R2 | Service `radar` : planification, jobs, budget X + tests | contrats, R1 (ou contrat seul) |
| B1 | Endpoints gate : curseurs SSE, `/v1/llm/usage`, permission + tests | contrats |
| B2 | Bot : client gate, gestionnaires, i18n, notificateur + tests | contrats, B1 |
| C1 | Réservations orphelines (routeur et radar) + tests | contrats |

Lance R1, B1 et C1 ensemble, puis R2 et B2. Pendant ce temps, rédige la
documentation. Relis les diffs et lance les tests ; corrige par un brief court si
nécessaire, sans refaire toi-même le travail délégué.

**Phase C — Intégration (toi)**
1. Suite complète : `ruff format --check`, `ruff check`, `mypy` strict, `pytest`,
   `alembic check`, `tower migrate`, puis `--downgrade base` et à nouveau `migrate`.
2. Smoke test des vrais processus, avec gate en `TOWER_HOST=0.0.0.0` (le bac à sable
   n'a pas de double pile IPv4/IPv6) et `curl --noproxy '*'` :
   - hangar avec `ping,ingest,radar` et un faux serveur X local (petit serveur HTTP
     de test pointé par une variable `TOWER_X_API_BASE`, défaut l'API officielle),
     pour montrer un cycle lecture → ingestion → `content.ingested` → planification
     suivante ;
   - le bot ne peut pas se connecter à Discord sans token : démontre le notificateur
     et les gestionnaires contre le vrai gate par un script, et mesure le RSS
     d'intercom en démarrant le processus jusqu'à l'échec d'authentification attendu
     (ou avec un mode `--dry-run` qui ne se connecte pas).
3. Mesures RSS et tests d'isolation.
4. Cohérence de la documentation, `docs/errors.md` régénéré.
5. Résumé de session (résultats réels, échecs compris, non vérifié), décisions,
   commits en anglais à l'impératif, push.

## 7. Économie d'usage (forfait Claude de Jules)

Chaque token consommé par toi ou tes sous-agents compte. Sois **efficace et économe,
sans rien céder sur la qualité** :

- **Répartition** : toi (Opus 5.5), tu conçois, relis et intègres ; les sous-agents
  Sonnet 5.5 implémentent. Aucun sous-agent Opus, aucun sous-agent d'exploration.
- **Lecture ciblée** : seulement les sections utiles (`grep`, offset et limite) ; ne
  relis pas ce que tu viens d'écrire ; dans les briefs, donne des chemins plutôt que
  de recopier des fichiers.
- **Briefs** précis pour éviter les allers-retours ; rapports de 10 lignes au plus.
- **Le moins de sous-agents possible** (les cinq du tableau) ; corrections
  regroupées en un seul brief de suivi.
- **Appels d'outils indépendants** regroupés dans un même tour.
- **Sorties courtes** (`pytest -q`, `| tail -n 30`) ; tests ciblés pendant le
  développement, suite complète aux points de contrôle seulement.
- **Recherche web** limitée à la documentation officielle de X et de discord.py pour
  les noms exacts.
- **Pas de réécriture complète** de fichiers, pas de documentation en double, pas de
  travail hors périmètre.
- **Messages à Jules courts**, un point par phase.

## 8. Rapport final attendu

En français, court :
- ce qui est livré ;
- une estimation de l'usage (sous-agents lancés, allers-retours) ;
- les vérifications avec leurs chiffres (tests, RSS avant et après) ;
- ce qui n'a pas pu être vérifié (au minimum : aucun appel réel à X ni à Discord) ;
- les écarts au plan et leur raison ;
- les questions pour Jules : clé X, application et token Discord, identifiants des
  salons, plafond mensuel X souhaité ;
- la prochaine partie proposée : lot 3, MVP du fact-check sur les comptes suivis.
