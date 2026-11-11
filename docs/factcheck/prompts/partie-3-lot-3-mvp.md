# Prompt d'implémentation — Partie 3 : lot 3, MVP du fact-check

Prompt à donner tel quel à une nouvelle session Claude Code (Opus 5.5). Il ne suppose
aucune connaissance préalable du projet. Rédigé le 2026-11-11, après la livraison des
parties 1 et 2 (PR #4 et #5 fusionnées dans `main`).

---

Tu es l'agent principal (Claude Opus 5.5) chargé d'implémenter le **lot 3 : le MVP
du fact-check**. Ce sera le premier fact-check de bout en bout dans le backend
« 12A Tower ». Tu orchestres des sous-agents **Claude Sonnet 5.5** et tu restes
responsable de la qualité, de l'intégration et des vérifications. Tu ne connais pas
encore le projet : la section 1 t'en donne l'essentiel, la documentation du dépôt fait
foi.

**Cette session développe sans clés d'API.** Les modèles (OpenAI, Anthropic) et les
sources payantes ou à inscription (Brave, Google Fact Check Tools, Légifrance, INSEE)
sont remplacés par des doublures. Seules **Wikipédia et Wikidata**, ouvertes et sans
clé, peuvent être appelées pour de vrai pendant le smoke test. La **qualité des
verdicts** sera mesurée et réglée dans une session ultérieure, avec les clés et le jeu
de référence : ton travail doit rendre cette session courte (outil d'évaluation prêt,
prompts soignés, réglages en configuration).

## 1. Le projet en bref

**Seat 12A** est un service de fact-checking et de contexte sur les réseaux sociaux.
Il est francophone, mais couvre tous les pays et toutes les langues. Son écosystème
comprend un site public, une extension Chromium et un bot Discord pour l'équipe. Le
développeur principal est Jules.

**12A Tower** est le backend : Python 3.13 asyncio, un codebase, une image Docker,
plusieurs processus sur Railway.

| Processus | Rôle |
|---|---|
| `tower gate` | API FastAPI unique, flux SSE |
| `tower hangar` | Worker : jobs, abonnements et tâches périodiques des services activés (`TOWER_SERVICES`) |
| `tower intercom` | Bot Discord, client de gate uniquement |
| `tower migrate` | Migrations |

**PostgreSQL est la seule infrastructure** : données, outbox d'événements avec
LISTEN/NOTIFY, file de jobs en `SKIP LOCKED`, verrous consultatifs.

Principes non négociables :
- **RAM** : Railway facture environ 10 $ par Go et par mois, donc imports paresseux,
  aucun modèle local, caches bornés.
- **Coût minimal**.
- **Scalabilité** par réplicas sans état.
- **Langues** : tout contenu porte sa langue ; l'équipe lit le français et
  l'anglais.
- **Multi-plateforme**.
- **Objectivité et égalité de traitement**, sans faux équilibre : un fait établi
  n'est jamais présenté comme débattu.
- **Aucune publication automatique** : rien n'est publié sans relecture humaine.
- **Aucun scraping de X**.

### Déjà fait

- **Socle** : dossiers (`received → in_review → validated/rejected → published`,
  « Marquer comme publié » manuel), outbox et SSE, file de jobs, services
  enfichables, auth (tokens de service, staff via `X-Acting-Discord-Id`, rôles
  `reviewer`, `editor`, `admin`), erreurs `TWR-*`.
- **Partie 1** (`docs/llm.md`, `docs/ingestion.md`) :
  - routeur de modèles `LLMRouter.complete(LLMRequest)` : routes par rôle et niveau
    dans `llm_routes`, cache de contenu `llm_calls`, allocation gratuite OpenAI,
    budgets réservés, prompts versionnés dans
    `src/tower/services/<service>/prompts/<prompt_id>.md` ;
  - ports et adaptateurs (`core/ports`, `core/adapters` : OpenAI, Anthropic,
    embeddings, `http_fetcher` sûr) ;
  - ingestion multi-plateforme (`content_items`, langue, SimHash,
    `content.ingested`, liste de suivi).
- **Partie 2** (`docs/radar.md`, `docs/intercom.md`) :
  - service hangar `radar` : lecture des comptes suivis sur X, planification
    adaptative, budget `provider:x`, `XPlatformClient` avec `get_item` ;
  - bot `tower intercom` : commandes `/watchlist`, `/dossier`, `/incidents`,
    `/usage`, catalogues `fr` et `en`, notificateur SSE avec curseur stocké côté
    gate ;
  - réservations de budget orphelines libérées automatiquement.

Le **plan complet** est dans `docs/factcheck/`. Ce lot correspond au « Lot 3 » de
`docs/factcheck/feuille-de-route.md`.

## 2. Lire avant toute action

1. `CLAUDE.md` (règles ; elles priment sur ce prompt).
2. `docs/sessions/2026-11-11-partie-2-radar-intercom.md` et
   `2026-11-11-partie-1-fondations-ingestion.md` (état, points ouverts).
3. Le plan, en entier pour ces pages, parce que c'est le cœur du lot :
   - `docs/factcheck/pipeline.md` : étages E2 et E6 à E11, contrôles, confiance ;
   - `docs/factcheck/contexte.md` : deux axes, 15 types de contexte, cadrage, note,
     brouillon Discord ;
   - `docs/factcheck/biais.md` et `docs/factcheck/objectivite.md` ;
   - `docs/factcheck/connaissances.md` : sources, Wikipédia, registre, droit
     d'auteur ;
   - `docs/factcheck/modeles.md` : rôles et routes ;
   - `docs/factcheck/niveaux.md` : profondeur uniforme, escalade, quota de
     brouillons ;
   - `docs/factcheck/international.md` : langues de publication ;
   - `docs/factcheck/evaluation.md` ;
   - `docs/factcheck/reponses-fable.md` : règles arbitrées, notamment Q3 et Q11.
4. `docs/decisions.md` (sections fact-check), `docs/llm.md`, `docs/intercom.md`,
   `docs/api.md`, `docs/events.md`, `docs/dossiers.md`, `docs/database.md`.
5. Le code : `core/llm/`, `core/ports/`, `core/adapters/`, `core/usecases/`,
   `services/radar/`, `intercom/`, `gate/routes/`, `tests/conftest.py`.

Ne refais pas les recherches documentées. Si le plan te paraît faux ou
contradictoire, note-le et pose la question à Jules au lieu de le changer en silence.

## 3. Périmètre

### Inclus (lot 3)

Pipeline pour les **comptes suivis (niveau N3)** et les **demandes manuelles de
l'équipe (niveau N4)** :

| Étage | Contenu |
|---|---|
| Déclenchement | Abonnement à `content.ingested` : un contenu de compte suivi actif (N3), pas une copie, langue traitée → job d'extraction. Demande manuelle (commande `/factcheck` et endpoint) → lecture du post par `PlatformClient.get_item`, ingestion, puis pipeline en N4 |
| E2 Extraction | 1 post par appel (rôle `extract`) : affirmations canoniques dans la langue d'origine et en français, signature normalisée, type, juridictions, vérifiabilité, indices de contexte, contexte du fil reformulé, motif d'absence fermé ; contrôle de fidélité dans les deux sens |
| E6 Cadrage | Rôle `frame` (Sonnet ; Opus en profondeur renforcée) |
| E7 Collecte | Requêtes symétriques (rôle `query_writer`), Atlas minimal, recherche web bornée, récupération sûre, passages, dédoublonnage, classement |
| E8 Jugement | Juge (rôle `judge`), second avis aveugle (rôle `second_opinion`, autre fournisseur), objection typée si désaccord de fond (rôle `objection`), escalade en profondeur renforcée, seconde passe sur désaccord |
| E9 Rédaction | Rôle `writer` : note de contexte (≤ 280 caractères) dans la langue du post + version de référence française (+ anglaise si le post est en anglais), fiche courte ; critique de neutralité (rôle `critic`, autre fournisseur) ; contrôles déterministes |
| E10 Assemblage | Confiance calculée (régression logistique à coefficients posés à la main), revérification du post, ouverture du dossier, `draft.ready`, quota de 50 brouillons par jour |
| E11 Revue | Brouillon affiché dans Discord avec boutons (valider, corriger, rejeter) ; décisions enregistrées comme données d'apprentissage ; traductions à la validation ; « Marquer comme publié » reste manuel |
| Lecture publique | Endpoint public qui donne le verdict **validé** d'un contenu, dans la langue demandée (fr, en ou langue du post), pour le site et l'extension |
| Évaluation | Commande `tower factcheck eval` prête pour la session de réglage |

### Exclu (lots suivants)

Embeddings stockés et pgvector, regroupement des affirmations, cache de verdicts,
fiches de référence, tranches de valeurs (lot 4) ; flux de masse N1/N2,
vérification légère E5, ordonnanceur, récits et vagues (lot 5) ; miroir LEGI, EUR-Lex
et autres pays, registre complet des sources et `controlled_by`, règle « partie
prenante » complète, faits établis (lot 6) ; médias (lot 8) ; audit Fable (lot 9) ;
API Batch.

Prévois le schéma pour que le lot 4 ajoute le regroupement **sans réécriture** : au
lot 3, chaque affirmation crée son propre groupe.

## 4. Spécifications

### 4.1 Données (une migration `0005`, expand)

Reprends les définitions de `docs/factcheck/donnees.md`, en ne créant que ce que le
lot 3 utilise :
- `claims` : texte d'origine et français, signature en colonnes typées, type,
  juridictions, entités avec rôle, vérifiabilité, indices de contexte, sans
  embedding ;
- `content_claims` ;
- `claim_groups` (niveau, état, verdict actif) et `claim_group_members` ;
- `investigations` (étape, profondeur, coût cumulé, cadrage en JSONB) ;
- `sources` (URL canonique et d'origine, éditeur, domaine, niveau, titre, dates,
  empreinte, licence) ;
- `evidence_passages` (texte, langue, `translation_fr`, origine attribuée, SimHash,
  conservé) ;
- `verdicts` (véracité, besoin de contexte, types de contexte, confiance, versions
  de prompts et de routes, statut, un seul actif par groupe) ;
- `citations` (verdict ↔ passage, citation exacte, assertion) ;
- `context_notes` (type, régime, texte, langue, statut, `expires_at`, résultats des
  contrôles) ;
- `context_note_translations` (origine `original`, `human` ou `machine`) ;
- `reviews` (décision, issue finale, diff, étiquettes, temps de relecture) ;
- `dossier_claim_groups` ;
- `source_registry` minimal (domaine → niveau, type, pays ; amorcé par une commande
  avec une courte liste d'institutions primaires françaises et internationales et
  d'agences de presse ; tout le reste est tertiaire par défaut) ;
- `source_cache` (requête ou URL hachée → réponse, date, durée de validité), pour ne
  jamais repayer une même requête.

Statuts en `TEXT` + `CHECK`, IDs en chaînes dans l'API.

### 4.2 Domaine pur (`core/domain/factcheck/`, bibliothèque standard)

- **Valeurs fermées** : issues de véracité (`true`, `mostly_true`, `misleading`,
  `false`, `unverifiable`), besoin de contexte (`none`, `useful`, `necessary`), les
  15 types de contexte de `contexte.md` avec leur régime, types d'affirmation,
  motifs d'absence, types d'objection, profondeur (`standard`, `reinforced`).
- **Comparaison du second avis** : même issue, écart de style (un cran du même côté)
  ou désaccord de fond (changement de côté, ou verdict contre abstention).
- **Décision d'escalade** : désaccord de fond, sources contradictoires ou confiance
  < 0,6 ; N4 toujours renforcé.
- **Confiance** : régression logistique, coefficients de `pipeline.md` (E10) en
  configuration, sigmoïde plafonnée à 0,90, seuils 0,80 et 0,55.
- **Indépendance** : deux origines indépendantes dont une primaire ; même origine
  attribuée = une seule ; le domaine sert de repli ; passages quasi identiques
  (SimHash) = une seule source.
- **Contrôles d'E9** :
  - citation présente mot pour mot dans le passage, après normalisation des espaces
    et des guillemets, et **contre le texte original, jamais une traduction** ;
  - longueur de la note ;
  - lexique chargé (liste en configuration, par langue), qualifications établies
    autorisées ;
  - chaque lien vient d'une source récupérée ;
  - dates des sources.
  « Vrai » ou « Faux » sans deux origines indépendantes dont une primaire est
  rétrogradé en « Non vérifiable » avec contexte.
- **Fidélité d'E2 dans les deux sens** : chiffres et entités de l'affirmation
  présents dans le post ; post à chiffres sans affirmation chiffrée → rejeu.
- **Passages** : découpage d'environ 120 mots, classement lexical en Python pur
  (type BM25), sélection des meilleurs, déduplication par SimHash.
- **Marquage du texte non fiable** (« datamarking ») pour les posts et les passages.
- **Quota de brouillons** : 50 par jour par défaut, priorité aux cas de doute, puis
  aux issues `false` et `misleading`.

Tests unitaires exhaustifs.

### 4.3 Prompts et schémas de sortie

**Tu écris toi-même les prompts** : leur qualité fait celle des verdicts, c'est le
meilleur usage de ton niveau. Fichiers en anglais dans
`src/tower/services/factcheck/prompts/` et `src/tower/services/investigate/prompts/` :
`extract`, `frame`, `query_writer`, `judge`, `second_opinion`, `objection`, `writer`,
`critic`, `translate`. Un modèle pydantic de sortie strict par rôle.

Exigences tirées du plan :
- **Extraction** : formulation neutre (pas d'euphémisme ni de dysphémisme),
  affirmations vérifiables seulement, signature extraite du texte d'origine dans des
  champs neutres en langue, aucun auteur.
- **Cadrage** : sens littéral, sous-entendus, interprétation charitable, définitions,
  période résolue (date du jour et date du post fournies), conditions de vérité et
  de fausseté, pièges, sous-questions, décision « vérifiable ou non ».
- **Requêtes** : séries « confirmerait » et « infirmerait » **symétriques**, dans la
  langue des sources visées.
- **Juge** :
  - preuves seulement : aucune connaissance interne comme preuve ;
  - citation exacte et identifiant de passage pour chaque assertion ;
  - origine de chaque passage ;
  - ce que les sources ne disent pas, points à vérifier, demande de recherche
    supplémentaire ;
  - **pas de score de confiance** ;
  - **pas de fausse symétrie** quand les preuves sont univoques ;
  - abstention possible si les preuves manquent.
- **Second avis** : mêmes entrées, passages dans un autre ordre, **sans le
  verdict**. **Objection** : liste fermée, identifiant de passage, direction fermée.
- **Rédaction** : la note commence par l'information manquante, ne porte aucun
  jugement sur l'auteur ni sur son intention, et renvoie aux sources ; le verdict
  est figé.
- **Critique** : grille de pontage (utile et juste pour des lecteurs d'avis
  opposés ? opinion, généralisation, mot chargé, fait non sourcé ?). Il signale, il ne
  réécrit pas.
- **Traduction** : fidèle, sans ajout.
- Les modèles ne reçoivent **jamais** l'auteur, son bord, l'engagement ni le niveau.
  Un test vérifie que les entrées des rôles `frame`, `judge`, `second_opinion` et
  `objection` ne contiennent aucun de ces champs.

Ajoute les routes manquantes à `tower llm seed` (par exemple `translate`) en
respectant `docs/factcheck/modeles.md` : petits rôles chez OpenAI, `frame`, `judge`
et `writer` chez Anthropic, `second_opinion`, `objection` et `critic` chez OpenAI.

### 4.4 Atlas minimal (`core/adapters/knowledge/`, imports paresseux)

Implémente les ports `KnowledgeSource` et `SearchClient` existants. **Vérifie chaque
API dans sa documentation officielle** : endpoints, paramètres, authentification,
limites. Consigne ce que tu as vérifié.
- **Wikipédia** :
  - API REST et Action API, User-Agent `TOWER_HTTP_USER_AGENT`, au plus 3 requêtes
    simultanées, respect de `Retry-After` ;
  - révision figée (on cite un numéro de révision) ;
  - contrôle du risque d'annulation (modèle « revert risk » de Wikimedia Lift Wing)
    pour une révision de moins de 48 h ;
  - extraction des **références** de l'article vers les sources primaires ;
  - édition de la langue de la juridiction, puis anglais et français.
- **Wikidata** : recherche d'entité, déclarations avec références et qualificatifs
  (une valeur sans référence compte comme tertiaire) ; scission du graphe depuis 2025.
- **Google Fact Check Tools** (`claims:search`, clé `TOWER_GOOGLE_API_KEY`).
- **Brave Search** (clé `TOWER_BRAVE_API_KEY`) : recherche d'abord limitée aux
  domaines primaires de la juridiction, puis élargie ; nombre de requêtes borné par la
  profondeur (4 en standard, +4 en renforcé).
- **Légifrance via PISTE** (OAuth2 client credentials, `TOWER_PISTE_CLIENT_ID` et
  `TOWER_PISTE_CLIENT_SECRET`) : recherche et consultation d'article, avec date de
  vigueur.
- **INSEE BDM** (SDMX, identifiants en configuration) : valeur structurée
  (indicateur, valeur, période, date de publication).
- **Sélecteur de sources** : choix des adaptateurs selon le type d'affirmation et
  la juridiction. Ordre : fact-checks existants, sources structurées, Wikipédia, puis
  recherche web.
- `source_cache` devant chaque appel. Une source indisponible ne bloque pas
  l'enquête : on passe à la suivante et l'erreur est capturée.

Tests avec `httpx.MockTransport` uniquement. Aucun réseau dans les tests.

### 4.5 Services et jobs

- **Service `factcheck`** (file `fc-fast`) :
  - abonnement `content.ingested` (court : il filtre et enfile) ;
  - job `factcheck.extract` ;
  - job `factcheck.assemble` (E10) ;
  - job `factcheck.request` (demande manuelle) ;
  - job `factcheck.translate` (à la validation).
- **Service `investigate`** (file `fc-deep`) : un job par étape (`frame`,
  `collect`, `judge`, `second_opinion`, `objection`, `escalate`, `draft`), pour
  qu'un crash ne fasse pas repayer les étapes faites. Étape courante dans
  `investigations.stage`, idempotence par `dedupe_key`.
- **Budgets** :
  - portée `level:N3` ou `level:N4` en plus de `global` ;
  - plafond par enquête (0,10 $ en standard, 0,40 $ en renforcé, en
    configuration) : au-delà, pas d'escalade ;
  - budget épuisé → `RetryLater`, et brouillon signalé si l'enquête ne peut
    finir.
- **Revérification** (E10) : `PlatformClient.get_item` du contenu représentatif
  (coût imputé à `provider:x`) ; post supprimé ou modifié → signalé dans le
  brouillon.
- **Conservation** : à la fin de l'enquête, seuls les passages cités et les liens
  des sources restent ; le texte des pages non citées est supprimé.
- **Événements** à ajouter à `docs/events.md` :
  - `claim.extracted` ;
  - `investigation.requested` et `investigation.completed` ;
  - `draft.ready` ;
  - `draft.reviewed` ;
  - `verdict.published`, à la validation (le dossier passe à `validated`, la
    publication sur X restant manuelle).

### 4.6 Gate et bot

**Gate** :
- `POST /v1/factcheck/requests {platform, external_id | url}` (`editor`) ;
- `GET /v1/drafts?status=pending` et `GET /v1/drafts/{id}` (`reviewer`) ;
- `POST /v1/drafts/{id}/review` avec `{decision: accept|edit|reject, veracity?,
  note_text?, labels?, reason?}` : `reviewer` pour accepter, `editor` pour valider,
  selon le modèle de permissions existant ;
- endpoint public `GET /v1/content/{platform}/{external_id}/verdict?lang=` (verdict
  et note **validés** seulement, traduction automatique signalée, cache HTTP).

Les cas d'usage vérifient les permissions, écrivent l'audit et publient
l'événement dans la même transaction.

**Bot** :
- `draft.ready` → message dans le salon des dossiers :
  - en-tête : issue, besoin de contexte, confiance, niveau ;
  - affirmation et lien du post ;
  - note proposée ;
  - justification avec citations et liens ;
  - ce que les sources ne disent pas ;
  - points à vérifier, dont l'objection éventuelle ;
  - boutons Valider, Corriger (fenêtre de saisie : issue, texte de la note, motif)
    et Rejeter (motif).
- `/factcheck <url>` pour une demande manuelle.
- Au démarrage, **réconciliation** : les brouillons en attente encore non affichés
  le sont. Cela corrige aussi la limite connue du notificateur (événements manqués au
  tout premier démarrage).
- Catalogues `fr` et `en` complétés, avec un test de parité ; contraintes RAM de
  `docs/intercom.md` inchangées.

### 4.7 Langues

- Affirmations, passages et notes portent leur langue.
- Note dans la langue du post + version de référence française (+ anglaise si le
  post est en anglais).
- Les relecteurs voient le français, ou l'anglais selon leur langue Discord.
- Les passages qui ne sont ni en français ni en anglais reçoivent une traduction
  française pour le relecteur (rôle `translate`, petit modèle).
- Les citations restent vérifiées sur l'original.
- À la validation, les versions fr, en et langue du post sont produites ; les
  traductions automatiques sont marquées comme telles.

### 4.8 Évaluation (prête pour la session de réglage)

- Format JSONL du jeu de référence (affirmation, langue, juridiction, date,
  véracité attendue, besoin de contexte attendu, types de contexte, notes), documenté
  dans `docs/factcheck/evaluation.md`. Fournis 5 cas d'exemple dans `tests/fixtures/`,
  clairement marqués comme exemples et non comme jeu de référence.
- `tower factcheck eval --set <fichier> [--limit N] [--report <fichier.md>]` :
  exécute E6 à E10 sur chaque cas, puis calcule l'exactitude, le taux de **faux
  confiants**, l'abstention, la validité des citations, le taux d'escalade, le coût
  moyen et la latence. Tous les appels passent par le cache de contenu, donc un
  rejeu ne repaie rien.
- Un mode « fournisseur factice » pour le développement (`TOWER_LLM_FAKE=1`, refusé
  quand `TOWER_ENV=prod`) produit des sorties valides par rôle. Il sert au smoke
  test et à démontrer `eval` sans clés.

### 4.9 Codes, configuration, documentation

- Codes : domaines `TWR-FCK-*` (pipeline) et `TWR-ATL-*` (sources), puis
  régénérer `docs/errors.md`.
- Variables (clés ci-dessus, plafonds par enquête, quota de brouillons,
  coefficients et seuils de confiance, langues) dans `docs/configuration.md` et
  `.env.example`.
- Documentation :
  - nouvelle page `docs/fact-check.md` (implémentation : flux, jobs, données,
    réglages, évaluation) ;
  - mises à jour de `api.md`, `events.md`, `database.md`, `auth.md`,
    `services.md`, `intercom.md`, `architecture.md`, `roadmap.md`,
    `docs/factcheck/feuille-de-route.md` (lot 3 livré en développement, réglage à
    faire), `docs/README.md`, `docs/decisions.md` ;
  - résumé de session `docs/sessions/AAAA-MM-JJ-lot-3-mvp.md`.

## 5. Exigences transversales

- **RAM** :
  - SDK, httpx et sources importés paresseusement ;
  - test d'isolation : gate n'importe pas `openai`, `anthropic` ni les adaptateurs
    de sources ; hangar sans `factcheck` ni `investigate` non plus ;
  - mesure du RSS au repos et après une enquête factice de hangar
    `ping,ingest,radar,factcheck,investigate`, de gate et d'intercom, avant et
    après. Références : gate 84 Mo, hangar 72 à 80 Mo, intercom 77 Mo. Cibles :
    gate au plus +5 Mo ; hangar au plus +20 Mo au repos (rapporte aussi la valeur
    après une enquête) ; intercom au plus +10 Mo ;
  - pages traitées en flux, HTML jeté après extraction (bibliothèque légère ou
    `html.parser` de la bibliothèque standard, mesurée) ;
  - aucun numpy, pandas ni lxml.
- **Économie d'argent** :
  - cache des appels et des sources ;
  - sources gratuites avant la recherche payante ;
  - recherches bornées par la profondeur ;
  - profondeur renforcée seulement sur difficulté ;
  - plafonds par enquête, par niveau et global.
- **Scalabilité** : jobs idempotents par étape, aucun état en mémoire, contre-pression
  par files ; le service `investigate` peut tourner sur un réplica dédié.
- **Sécurité** : texte non fiable délimité et marqué, sorties validées par schéma,
  aucun outil donné aux modèles, `WebFetcher` sûr pour toute URL.
- **Socle** : cas d'usage pour toute écriture, événements dans la même transaction,
  services n'important que `tower.core`, abonnements courts, migrations
  expand/contract.
- **Code en anglais, documentation en français.**

## 6. Méthode de travail

**Branche** : pars de `main`, sur la branche indiquée par ta session. Pas de pull
request sans demande explicite de Jules.

**Base de test** : PostgreSQL 16 réel (voir `docs/development.md` ; tests sur
`postgresql://postgres@localhost:5433/tower_test`). Installe-le s'il manque. Ne simule
jamais la base.

**Phase A — Contrats et prompts (toi seul)**
1. Vérifie que les 536 tests passent.
2. Écris les contrats :
   - modèles SQLAlchemy et migration `0005` ;
   - valeurs fermées et signatures du domaine ;
   - modèles de sortie pydantic par rôle ;
   - signatures des adaptateurs de sources ;
   - noms des jobs, topics, codes, variables, permissions.
3. Écris les **prompts** (section 4.3).
4. Vérifie `alembic check`, `ruff` et `mypy`, puis commit.

**Phase B — Sous-agents Claude Sonnet 5.5** (`claude-sonnet-5-5` ; dans l'outil
Agent, `model: "sonnet"` ; vérifie au premier lancement que c'est bien Sonnet 5.5).

Chaque brief est **autonome** et contient :
- les fichiers autorisés, et seulement ceux-là ;
- les signatures exactes ;
- le comportement et les cas limites ;
- les tests à écrire ;
- les commandes (`uv run ruff format`, `uv run ruff check`, `uv run mypy`,
  `uv run pytest` ciblé) ;
- les interdits : aucun commit, aucun fichier hors liste, aucun import lourd au
  niveau module, aucun réseau dans les tests ;
- un rapport de 10 lignes au plus.

| Agent | Contenu | Dépend de |
|---|---|---|
| F1 | Domaine pur (4.2) + tests | contrats |
| F2 | Atlas minimal, `source_cache`, sélecteur de sources (4.4) + tests | contrats |
| F3 | Service `investigate` : étapes E6 à E9, escalade, conservation (4.5) + tests avec fournisseurs et sources factices | F1, F2 (ou contrats) |
| F4 | Service `factcheck` : déclenchement, E2, E10, demande manuelle, quota, revérification, traductions ; mode fournisseur factice (4.5, 4.8) + tests | F1 |
| F5 | Gate (brouillons, revue, demande, lecture publique) + bot (message, boutons, réconciliation, `/factcheck`, i18n) (4.6) + tests | contrats |
| F6 | `tower factcheck eval`, format JSONL, exemples (4.8) + tests | F3, F4 |

Lance F1, F2 et F5 ensemble, puis F3 et F4, puis F6. Pendant ce temps, rédige la
documentation. Relis les diffs et lance les tests ; corrige par un brief court si
nécessaire.

**Phase C — Intégration (toi)**
1. Suite complète : ruff, mypy strict, pytest, `alembic check`, migrations aller et
   retour.
2. Smoke test des vrais processus avec `TOWER_LLM_FAKE=1`, gate en
   `TOWER_HOST=0.0.0.0` et `curl --noproxy '*'` :
   - compte suivi → post ingéré (faux serveur X local, comme à la partie 2) →
     extraction → enquête (Wikipédia et Wikidata **réels**, autres sources
     factices) → brouillon → `draft.ready` en SSE ;
   - revue par l'endpoint → `verdict.published` → lecture publique du verdict en
     fr, en et langue du post ;
   - démontre aussi le bot contre le vrai gate par script (sans Discord) ;
   - lance `tower factcheck eval` sur les 5 exemples.
3. Mesures RSS et tests d'isolation.
4. Cohérence de la documentation, `docs/errors.md` régénéré, résumé de session
   (résultats réels, échecs, non vérifié), décisions, commits en anglais, push.

## 7. Économie d'usage (forfait Claude de Jules)

Chaque token compte. Sois **efficace et économe, sans rien céder sur la qualité** :

- **Répartition** :
  - toi (Opus 5.5), tu conçois, écris les prompts, relis et intègres ;
  - les sous-agents Sonnet 5.5 implémentent ;
  - aucun sous-agent Opus, aucun sous-agent d'exploration.
- **Lecture ciblée** : seulement les sections utiles ; ne relis pas ce que tu viens
  d'écrire ; dans les briefs, des chemins plutôt que des copies de fichiers.
- **Briefs** précis pour éviter les allers-retours ; rapports de 10 lignes au plus ;
  le moins de sous-agents possible (les six du tableau) ; corrections regroupées.
- **Outils** : appels indépendants regroupés dans un même tour ; sorties courtes
  (`pytest -q`, `| tail -n 30`) ; suite complète aux points de contrôle seulement.
- **Recherche web** limitée à la documentation officielle des API de sources et des
  SDK (skill `claude-api` pour Anthropic).
- **Pas de réécriture complète**, pas de documentation en double, rien hors
  périmètre.
- **Messages à Jules courts**, un point par phase.

## 8. Rapport final attendu

En français, court :
- ce qui est livré ;
- une estimation de l'usage (sous-agents, allers-retours) ;
- les vérifications avec leurs chiffres (tests, RSS avant et après) ;
- ce qui n'a pas pu être vérifié : au minimum aucun appel réel aux modèles ni aux
  sources à clé, et la qualité des verdicts non mesurée ;
- les écarts au plan ;
- ce qu'il faut à Jules pour la **session de validation** : clés Anthropic, OpenAI
  (organisation avec partage), Brave, Google, PISTE et INSEE, et au moins
  50 affirmations du jeu de référence ;
- la prochaine étape proposée : session de validation et de réglage, puis lot 4.
