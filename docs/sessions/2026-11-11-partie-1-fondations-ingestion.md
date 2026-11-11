# 2026-11-11 — Partie 1 du fact-check : fondations et ingestion

## Objectif

Implémenter les lots 1 (fondations : ports, adaptateurs, routeur de modèles, budgets)
et 2 (ingestion générique multi-plateforme) du plan de fact-check, selon
`docs/factcheck/prompts/partie-1-fondations-ingestion.md`. Session Opus 5.5
orchestrant six sous-agents Claude Sonnet 5.5 (modèle confirmé par chacun).

## Réalisé

- **Phase A (contrats, Opus)** : ports (`core/ports/{llm,embeddings,knowledge,web,platforms}.py`,
  `XClient` gardé compatible), signatures du domaine, `core/domain/net.py`, modèles
  `core/models/{llm,ingestion}.py`, migrations `0002` (LLM, budgets) et `0003`
  (ingestion, partitions hebdomadaires créées par un bloc `DO`), codes `TWR-LLM-*`,
  `TWR-ING-*`, `TWR-WEB-*`, variables de configuration, permissions
  (`content:ingest`, `watchlist:read`, `watchlist:write`, acteur `installation`),
  contrat `Service.periodic` (tâches singleton) branché dans hangar, filtre des
  partitions dans `migrations/env.py`, secret du jour (`core/usecases/installations.py`),
  points d'entrée CLI `tower llm` et `tower ingest`.
- **S1** : domaine pur (`platforms`, `text`, `language`, `privacy`, `llm`) et tests.
- **S2** : adaptateurs OpenAI (Responses), Anthropic (Messages), embeddings,
  `factory.py`, `core/llm/schema.py` (schéma strict).
- **S3** : `core/adapters/http_fetcher.py` (SSRF, IP épinglée par saut, robots.txt,
  plafond de 2 Mo), `FetchError`.
- **S4** : routeur `core/llm/{router,prompts,errors,admin}.py`, `tower llm seed|usage`.
- **S5** : `core/domain/ingestion.py`, cas d'usage `ingestion.py` et `watchlist.py`,
  routes `gate/routes/{ingest,watchlist}.py`, limiteur par IP `gate/ratelimit.py`,
  `publish_filters_from_file`.
- **S6** : service `services/ingest` (liaison des copies, partitions, secrets).
- **Intégration (Opus)** : test d'isolation des imports, correction du SimHash (URL et
  mentions exclues des traits, voir ci-dessous), documentation : nouvelles pages
  `docs/llm.md` et `docs/ingestion.md`, mises à jour de `database.md`, `api.md`,
  `auth.md`, `services.md`, `architecture.md`, `configuration.md`, `events.md`,
  `roadmap.md`, `factcheck/feuille-de-route.md`, `README.md`, `.env.example`,
  `errors.md` régénéré.

## Décisions

Toutes reportées dans `docs/decisions.md` (section « Partie 1 »). Principales :
adaptateurs partagés dans `core/adapters` ; repli fait par le routeur et non côté
serveur ; modèle à groupe gratuit jamais appelé en payant ; réservation atomique de
l'allocation gratuite ; un refus de budget passe au repli suivant (écart au texte
initial du plan, plus économe) ; domaine `TWR-WEB-*` ; pas de partition par défaut ;
`observed_at` borné à 24 h ; liaison des copies sans événement ; audit de la liste de
suivi dans `watched_account_events` (table ajoutée au plan) ; topic
`extension_filters.published` ajouté.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict (121 fichiers) : OK.
- `pytest` : **382 tests verts** (51 au départ), sur PostgreSQL 16 réel (installé et
  configuré sur le port 5433 dans le conteneur).
- `alembic check` OK ; `tower migrate`, `--downgrade base`, puis `migrate` : OK.
- Smoke test des vrais processus (`tower gate`, `tower hangar` avec
  `TOWER_SERVICES=ping,ingest`, base `tower`) : lot de 2 posts → `202`
  `{accepted 2, new 2}`, renvoi → `{new 0, duplicates 2}`, 2 événements
  `content.ingested` reçus en SSE (aucun au renvoi), 2 jobs `ingest.link_copies`
  réussis, copie liée (`copy_of_id`), `GET /v1/ingest/config` avec `ETag` et
  `Cache-Control`. **Premier essai en échec** : la copie (même texte + lien + mention)
  n'était pas liée, distance SimHash de 7 ; corrigé en excluant URL et `@user` des
  traits (distance 0, test ajouté). Note : gate écoute sur `::` et le conteneur n'a pas
  de double pile IPv4/IPv6 ; smoke test lancé avec `TOWER_HOST=0.0.0.0`.
- `tower llm seed` (12 modèles, 15 routes, budget global de 30 $) puis second passage
  idempotent (0 route créée) ; `tower llm usage` affiche l'allocation du jour et le
  budget du mois.
- **RSS au repos** (mesure après 7 s, même méthode avant et après) : gate 82 → 83-84 Mo
  (+1 à 2 Mo ; 93 Mo après quelques requêtes d'ingestion) ; hangar `ping` 69 → 68 Mo ;
  hangar `ping,ingest` 68 Mo. Cibles (+10 et +15 Mo) respectées.
- Test d'isolation : `import tower.gate.app` et hangar (`ping`, `ping,ingest`) ne
  chargent ni `openai` ni `anthropic` (hangar ne charge pas non plus FastAPI ni httpx).
- **Non vérifié** : aucun appel réel à OpenAI ni à Anthropic (pas de clé) : les
  formes des requêtes ont été contrôlées dans le code source des SDK installés
  (openai 3.49, anthropic 2.8) et la skill `claude-api`, et testées avec des faux
  clients ; `stop_details.category` d'Anthropic et l'attribut `_request_id` d'OpenAI
  sont lus défensivement. Pas de test de charge (20 000 contenus par jour) ni de mesure
  du rappel des quasi-copies sur un corpus. Rétention des sauvegardes Railway non
  vérifiée.

## Points ouverts / suite

- **Seuil SimHash** : une substitution d'un mot dans un texte de 40 mots donne une
  distance de 4 à 7 (mesures de S1), au-delà du seuil de 3 du plan ; seuls les ajouts
  de lien ou de mention sont neutralisés. À trancher sur un corpus réel : seuil plus
  large (6-8) avec confirmation par recouvrement de mots, ou autres traits.
- Réservations de budget orphelines si un processus meurt entre réservation et
  règlement : prévoir une récupération (tâche périodique) au lot 3.
- Jules : vérifier l'éligibilité GPT-6 à l'allocation gratuite (renseigner
  `free_group`), fournir les clés pour un premier appel réel des deux fournisseurs,
  vérifier la rétention des sauvegardes Railway (destruction réelle des secrets).
- Gate en local : `TOWER_HOST=0.0.0.0` nécessaire sur une machine sans double pile.
- Limite par IP propre à chaque réplica gate.
- Suite proposée : lot 3 (MVP sur les comptes suivis), qui demande les clés d'API,
  le radar X et le jeu de référence de 300 affirmations.
