# 2026-11-11 — Partie 2 : radar X et bot intercom

## Objectif

Préparer le lot 3 (MVP sur les comptes suivis) : lire les posts des comptes suivis sur
X (radar) et donner à l'équipe son outil de travail (bot Discord `tower intercom`) ;
régler les points ouverts de la partie 1 (réservations de budget orphelines) et
ajouter les endpoints utiles au bot. Session Opus 5.5 orchestrant cinq sous-agents
Claude Sonnet 5.5 (modèle confirmé par chacun).

## Réalisé

- **Phase A (contrats, Opus)** : port étendu (`PollingPlatformClient`, `PollPage`,
  `ReadUsage`, erreurs typées `Platform*`), squelettes de l'adaptateur X, du registre
  des budgets, du rapport d'usage, des curseurs SSE, du domaine radar et du client
  gate du bot ; migration `0004` ; codes `TWR-RAD-001..007`, `TWR-BOT-001..004`,
  `TWR-EVT-003`, `TWR-MNT-002` ; permission `usage:read` ; variables ; CLI
  `tower intercom [--dry-run]` et `tower llm budget <portée> <usd>` ; dépendance
  discord.py 2.7.1. Vérification de l'API X sur la spécification OpenAPI officielle.
- **R1** : `XPlatformClient` (`core/adapters/x_client.py`), 38 tests.
- **C1** : registre commun `core/budget.py` (réservations dans `budget_reservations`,
  règlement sans double décrément, libération des réservations expirées par la
  maintenance), routeur refactoré, 10 tests.
- **B1** : `GET/PUT /v1/events/cursors/{nom}`, `GET /v1/llm/usage`, `run_usage` sur la
  même source, `run_budget`, `provider:x` créé par le seed, ajout par pseudo et cas
  d'usage `resolve_watched_account`, 18 tests.
- **R2** : service hangar `radar` (planification adaptative, `radar.poll_account`,
  `radar.resolve_account`, abonnement `watchlist.changed`), domaine pur, ~42 tests.
- **B2** : bot `src/tower/intercom/` (client gate, gestionnaires purs, catalogues
  `fr`/`en`, notificateur, client discord.py, `railway/intercom.json`), 40 tests.
- **Intégration (Opus)** : ajustement du test du seed (2 compteurs), loggers `httpx`,
  `httpcore`, `discord` ramenés à `warning` (une ligne par requête sinon),
  `discord.client` à `error` (avertissements « voice » à chaque démarrage), échec de
  connexion Discord transformé en `TWR-BOT-004` propre (code 2) au lieu d'une trace.
- **Documentation** : nouvelles pages `radar.md` et `intercom.md` ; mises à jour de
  `architecture.md`, `services.md`, `configuration.md`, `.env.example`, `database.md`,
  `events.md`, `api.md`, `auth.md`, `llm.md`, `ingestion.md`, `deployment-railway.md`,
  `roadmap.md`, `factcheck/feuille-de-route.md`, `README.md` (racine et `docs/`),
  `CLAUDE.md`, `decisions.md` ; `errors.md` régénéré.

## Décisions

Reportées dans `docs/decisions.md` (section « Partie 2 »). Principales : radar en
service hangar (RAM) ; noms de paramètres X de la spécification actuelle
(`post.fields`, `referenced_posts`, `note_post`) avec lecture des anciens noms ;
pas d'expansion des posts référencés pendant le polling (facturés) ;
`exclude=retweets` ; radar sur `provider:x` seul (pas `global`) et jamais sans plafond
(compteur créé à 20 $) ; plafond quotidien calculé sur les jobs du jour ; bail à
l'enfilage ; une transaction finale par lecture (idempotence) ; colonnes de
planification sans incrément de `version` ; comptes en attente (`external_id`
nullable) ; curseurs SSE dans `event_cursors` ; codes `TWR-BOT-*` seulement
journalisés.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict (153 fichiers) : OK.
- `pytest` : **536 tests verts** (382 au départ), sur PostgreSQL 16 réel (port 5433).
  Pendant le travail parallèle, deux sous-agents ont eu des échecs transitoires dus à
  la base de test partagée (troncature par une autre session) ; R2 et B2 ont ensuite
  eu chacun leur base (`tower_test_r2`, `tower_test_b2`).
- `alembic check` OK ; `tower migrate --downgrade base` puis `migrate` OK ;
  `--downgrade 0003` avec un compte en attente présent : supprimé proprement, puis
  `migrate` OK.
- **Smoke test des vrais processus** (gate en `TOWER_HOST=0.0.0.0`, hangar
  `ping,ingest,radar`, faux serveur X local via `TOWER_X_API_BASE`) : ajout de
  `@Smoke_Account` par pseudo via gate → job `radar.resolve_account` → compte résolu
  (`watchlist.changed` resolved) ; planification → `radar.poll_account` : 2 posts lus
  (`post.fields`, `expansions`, `media.fields`, `max_results=20`, `exclude=retweets`
  vérifiés côté serveur), 2 contenus ingérés source `radar`, 2 `content.ingested`
  reçus en SSE, `since_cursor` mis à jour ; ajout d'un post → cycle suivant avec
  `since_id=…002` : 1 post, 3ᵉ `content.ingested` ; cycle vide suivant : intervalle
  60 → 120 s, `consecutive_empty=1`. Budget `provider:x` créé à 20 $, dépense
  0,015 $, `reserved=0`, aucune ligne `budget_reservations` restante ; `version` du
  compte suivi inchangée par le radar (2, due à la résolution). Aucun avertissement ni
  erreur dans les journaux.
- **Bot contre le vrai gate** (script, sans Discord) : `/watchlist list` (fr),
  `/usage` (en, `provider:x` affiché), `/incidents list`, `/dossier take`, erreur
  traduite `TWR-DOS-001` ; notificateur : premier démarrage sans curseur parti de
  « maintenant » (aucune inondation), `dossier.opened` notifié avec la plateforme,
  curseur stocké ; second démarrage : reprise au curseur, la transition survenue
  pendant l'arrêt est notifiée, sans doublon.
- **RSS au repos** (après 7 s, même méthode avant et après) :

  | Processus | Avant | Après | Cible |
  |---|---|---|---|
  | gate | 84 Mo | 84 Mo | ≤ +5 Mo ✅ |
  | hangar `ping,ingest` | 68 Mo | 69 Mo | — |
  | hangar `ping,ingest,radar`, aucun compte dû | — | 72 Mo (+3) | ≤ +10 Mo ✅ |
  | hangar `ping,ingest,radar`, après une lecture X | — | 80 Mo (+11) | ≤ +10 Mo ⚠️ |
  | intercom `--dry-run` (client discord.py construit, SSE connecté) | — | 77 Mo | ≤ 120 Mo ✅ |

  Le dépassement d'environ 1 Mo après une lecture vient du chargement de httpx et de
  son contexte TLS (import de httpx + adaptateur ≈ 4 Mo mesurés) : inévitable pour
  appeler X, et seulement sur le réplica qui active `radar`.
- Isolation des imports (tests) : gate et hangar `ping,ingest` ne chargent ni
  `discord` ni l'adaptateur X ; hangar `ping,ingest,radar` ne charge ni `httpx` ni
  l'adaptateur X ni FastAPI avant la première lecture ; `tower.core.adapters.x_client`
  n'importe pas httpx au chargement ; les gestionnaires et le notificateur du bot ne
  chargent pas `discord`.
- **Non vérifié** : aucun appel réel à l'API X (pas de clé ; noms vérifiés sur la
  spécification officielle seulement). Aucune connexion réelle à Discord : le
  lancement avec un faux token a fait **un** appel de connexion à l'API Discord, refusé
  (401), ce qui a permis de vérifier la sortie propre `TWR-BOT-004`. Les commandes
  slash n'ont pas été exécutées dans un vrai serveur Discord (synchronisation, rendu
  des options, limite de 3 s). RSS d'intercom connecté à Discord non mesuré (le dry-run
  ne charge pas l'état des serveurs ; attendu faible avec les caches désactivés).

## Points ouverts / suite

- Jules : clé X (jeton bearer) et accès « pay-per-use » ; application et token
  Discord, identifiant du serveur et des quatre salons ; plafond mensuel X souhaité
  (20 $ par défaut ; ordre de grandeur : 100 comptes à 5 posts par jour ≈ 75 $ par
  mois) ; vérifier au premier appel réel que l'API accepte `post.fields`.
- Le token du bot doit inclure `events:read` ; `/incidents` est réservé aux admins
  (seul rôle avec `errors:*`) : à confirmer.
- Notificateur : un flux coupé avant le tout premier événement repart de
  « maintenant » (limite documentée).
- gate n'a pas de lecture unitaire d'un compte suivi : le bot relit la liste (500 au
  plus) pour obtenir la `version`.
- Toujours ouverts depuis la partie 1 : seuil SimHash à trancher sur un corpus réel ;
  premier appel réel des fournisseurs de modèles ; rétention des sauvegardes Railway.
- Suite proposée : **lot 3, MVP du fact-check sur les comptes suivis** (extraction,
  cadrage, collecte, jugement, brouillons revus dans Discord), qui demande les clés
  d'API et le jeu de référence de 300 affirmations.
