# Prompt d'implémentation — Lot 4 : V1 (embeddings, groupes, cache, profils)

Prompt à donner tel quel à une session Claude Code (Opus 5.5) qui implémente le lot 4
du plan de fact-check. Rédigé le 2026-11-11, après le lot 3. À lancer **après** la
session de validation et de réglage du lot 3 : la réutilisation automatique des
verdicts suppose que leur qualité ait été mesurée.

---

Tu es l'agent principal (Claude Opus 5.5) chargé d'implémenter le **lot 4 : la V1 du
fact-check**. Il comprend les embeddings, le regroupement des affirmations, la
réutilisation des verdicts (cache), les fiches de référence, les profils de traitement
et les niveaux N0 à N4. Tu orchestres des sous-agents **Claude Sonnet 5.5** et tu restes
responsable de la qualité, de l'intégration et des vérifications. Tu ne connais pas
encore le projet : la section 1 t'en donne l'essentiel, la documentation du dépôt fait
foi.

**Clés d'API.** Si la session de validation du lot 3 a fourni les clés, utilise-les
pour les vérifications réelles (embeddings OpenAI compris), avec des budgets bas. Sinon,
développe comme au lot 3 avec les doublures (`TOWER_LLM_FAKE=1`, qui doit alors couvrir
aussi les embeddings). Dans les deux cas, dis clairement dans le rapport ce qui a été
appelé pour de vrai.

## 1. Le projet en bref

**Seat 12A** est un service de fact-checking et de contexte sur les réseaux sociaux,
francophone mais ouvert à tous les pays et à toutes les langues. Son écosystème comprend
un site public, une extension Chromium et un bot Discord pour l'équipe. Le développeur
principal est Jules.

**12A Tower** est le backend : Python 3.13 asyncio, un seul codebase, une seule image
Docker, plusieurs processus sur Railway.

| Processus | Rôle |
|---|---|
| `tower gate` | API FastAPI unique, flux SSE |
| `tower hangar` | Worker : jobs, abonnements et tâches périodiques des services activés (`TOWER_SERVICES`) |
| `tower intercom` | Bot Discord, client de gate uniquement |
| `tower migrate` | Migrations |

**PostgreSQL est la seule infrastructure** : données, outbox d'événements avec
LISTEN/NOTIFY, file de jobs en `SKIP LOCKED`, verrous consultatifs. Au lot 4 s'ajoute
**pgvector**.

Principes non négociables :
- **RAM** : Railway facture environ 10 $ par Go et par mois. Donc imports paresseux,
  aucun modèle local, aucun numpy, et des caches bornés.
- **Coût minimal**.
- **Scalabilité** par réplicas sans état.
- **Langues** : tout contenu porte sa langue.
- **Multi-plateforme**.
- **Objectivité et égalité de traitement**, sans faux équilibre.
- **Aucune publication automatique.**
- **Aucun scraping de X.**

### Déjà fait

- **Socle** : dossiers, outbox et SSE, file de jobs, services enfichables, auth (rôles
  `reviewer`, `editor`, `admin`), erreurs `TWR-*`.
- **Partie 1** : routeur de modèles (`docs/llm.md`) et ingestion multi-plateforme
  (`docs/ingestion.md`).
- **Partie 2** : radar X et bot `intercom` (`docs/radar.md`, `docs/intercom.md`).
- **Lot 3** (`docs/fact-check.md`) : fact-check de bout en bout pour les comptes suivis
  (N3) et les demandes manuelles (N4).
  - Tables (migration `0005`) : `claims`, `claim_groups` (un groupe par affirmation
    pour l'instant), `claim_group_members` historisée, `investigations`, `verdicts` (un
    seul actif par groupe), `context_notes`, `reviews`, `source_registry`,
    `source_cache`.
  - Services `factcheck` (E2, E10, traductions) et `investigate` (E6 à E9, un job par
    étape, `run_in_memory`).
  - Atlas minimal, domaine pur dans `core/domain/factcheck/`, revue dans Discord,
    lecture publique du verdict validé, `tower factcheck eval`.
- **Session de validation du lot 3** : voir son résumé dans `docs/sessions/`
  (résultats mesurés, réglages retenus, points ouverts).

Le plan complet est dans `docs/factcheck/`. Ce lot correspond au « Lot 4 » de
`docs/factcheck/feuille-de-route.md`.

## 2. Lire avant toute action

1. `CLAUDE.md` (règles ; elles priment sur ce prompt).
2. Les derniers résumés dans `docs/sessions/` : la validation du lot 3 et
   `2026-11-11-lot-3-mvp.md` (état, écarts, points ouverts).
3. `docs/fact-check.md` (ce qui est implémenté) et `docs/decisions.md` (sections
   fact-check et « Lot 3 »).
4. Le plan, **en entier** pour ces pages, qui sont le cœur du lot :
   - `docs/factcheck/pipeline.md` : E1, E3 (dont « Réutilisation d'un verdict »), E10 ;
   - `docs/factcheck/niveaux.md` : niveaux, réglages par niveau, promotion, quota,
     options écartées ;
   - `docs/factcheck/donnees.md` : `content_embeddings`, `claims.embedding`,
     `claim_groups`, `claim_group_events`, `reference_facts`, `verdict_bands`,
     `processing_profiles`, `level_overrides`, `router_models`, index et rétention ;
   - `docs/factcheck/reponses-fable.md` : Q7 (identifiants, index vectoriels) et Q9
     (compatibilité des signatures) ;
   - `docs/factcheck/contexte.md` : régimes de réutilisation des notes ;
   - `docs/factcheck/evaluation.md` : paires pièges, paires interlingues, métriques E3 ;
   - `docs/factcheck/couts.md` (démarrage à froid) et `docs/factcheck/international.md`
     (paires interlingues, vecteur pivot).
5. `docs/llm.md` (port `Embedder`, routes, budgets), `docs/database.md`,
   `docs/events.md`, `docs/api.md`, `docs/intercom.md`.
6. Le code : `core/domain/factcheck/`, `core/factcheck/`, `core/ports/embeddings.py`,
   `core/adapters/openai_embedder.py`, `core/adapters/fake_llm.py`,
   `core/usecases/{claims,assembly,drafts,investigations}.py`, `services/factcheck/`,
   `services/investigate/` (dont `evaluation.py`), `intercom/handlers/`,
   `gate/routes/factcheck.py`, `tests/conftest.py`.

Ne refais pas les recherches documentées. Si le plan te paraît faux ou contradictoire,
note-le et pose la question à Jules au lieu de le changer en silence.

## 3. Périmètre

### Inclus (lot 4)

| Brique | Contenu |
|---|---|
| pgvector | Extension ≥ 0.8 (`halfvec`). **Vérifie d'abord** qu'elle est disponible sur l'offre PostgreSQL de Railway (documentation officielle de Railway). Si elle ne l'est pas, arrête-toi et pose la question à Jules avant d'écrire la migration. Installe-la sur la base de test locale |
| Embeddings | Port `Embedder` existant, `text-embedding-3-small` à 512 dimensions, par lots, via le routeur (route `embed`, budget, cache). Doublure déterministe en mode factice : deux textes proches doivent donner des vecteurs proches, pour que le smoke test ait un sens |
| E1 Routage | Embedding des contenus des langues traitées, stocké 14 jours dans `content_embeddings` (partitions hebdomadaires, **sans index vectoriel**). Score « vérifiable » par régression logistique en Python pur, poids dans `router_models`. **Règle permissive** au démarrage (aucun modèle actif : tout passe). Niveau initial N0 ou N1 enregistré, et 2 % des contenus écartés tirés au hasard pour mesurer les faux négatifs |
| E3 Appariement | Pour chaque affirmation extraite : embedding (texte d'origine, plus le pivot français hors français), puis recherche des 5 groupes les plus proches (HNSW `halfvec_cosine_ops` sur le **vecteur représentatif des groupes**, seul index vectoriel). Ensuite : comparaison **déterministe** des signatures, résolution vers une fiche de référence, arbitre (rôle `pair_arbiter`) en zone grise et **toujours pour les paires interlingues** (sur les deux textes d'origine), recherche dans nos verdicts publiés et dans les fact-checks externes. Sortie : rattachement à un groupe existant ou création d'un groupe, avec un événement |
| Groupes | Fusion et scission **réversibles** (`claim_group_members` historisée, `claim_group_events`), par cas d'usage, depuis Discord. Choix de l'affirmation et du vecteur représentatifs. Une scission ne doit jamais propager un verdict à tort |
| Réutilisation des verdicts | Règles de Q9, strictes : 1) réutilisation directe seulement à signature normalisée **identique** ; 2) entre variantes chiffrées, jamais par similarité, mais par fiche et **bande de valeurs** (tolérance τ = max(plancher, 5 % de la référence), plancher de 0,2 point pour un taux) ; 3) une bande validée une fois par un relecteur s'applique ensuite aux variantes ; 4) période résolue vers la même fiche, avec `outdated` détecté sans modèle quand la valeur correspond à la fiche précédente. **N4 : jamais de réutilisation directe** (sources rechargées et rejugées). Un groupe couvert hérite sans relecture et n'occupe pas de place dans le quota |
| Péremption et revalidation | Durée de validité d'un verdict par type d'affirmation (en configuration). Note provisoire limitée à 6 h et jamais héritée. Verdict expiré : `verdict.expired`, groupe en `to_revalidate`. Job `factcheck.revalidate` (nouvelle enquête, ou recharge de la fiche pour une statistique) |
| Fiches de référence | `reference_facts` : indicateur, variante de définition, valeur, unité, période, territoire, source primaire, date de publication, prochaine publication, fiche précédente. Créées depuis une enquête validée (bouton du bot) ou par l'alimentation automatique INSEE pour une courte liste d'indicateurs suivis (en configuration). **Validées par un relecteur** avant de servir |
| Bandes | `verdict_bands` : fiche × bande × côté → issue validée. Validation depuis le brouillon ou depuis une commande du bot |
| Profils et niveaux | `processing_profiles` versionnés (réglages par niveau de `niveaux.md`, version active, auteur, retour arrière) avec **bornes de sécurité** vérifiées par le cas d'usage. `level_overrides` (cible : post, groupe, compte ; expiration ; motif). Budgets `level:N0..N4`. Tout est modifiable depuis Discord (`/profile`, `/level`) et par l'API |
| Gate et bot | Endpoints des groupes (lecture, fusion, scission), des fiches, des bandes, des profils et des forçages. Dans le bot : boutons sur le brouillon (« même groupe que… », « scinder », « créer une fiche », « valider la bande »), commandes `/group`, `/fact`, `/profile`, `/level`. La lecture publique sert aussi les variantes couvertes (verdict hérité, avec le lien vers le verdict d'origine) |
| Entraînement | `tower factcheck train-router` : distillation hors production du classifieur E1 à partir des décisions d'E2 et des relecteurs, avec un échantillonnage stratifié. Une nouvelle version n'est activée qu'après mesure |
| Évaluation | `tower factcheck eval` étendu : **taux de fausse réutilisation** sur les paires pièges (cible < 1 %), pureté des groupes, paires interlingues (même groupe attendu), part des affirmations tranchées sans recherche payante ; format JSONL complété et documenté |

### Exclu (lots suivants)

- Flux de masse N1/N2 : micro-lots d'extraction, promotion N2 (observateurs, vélocité),
  confirmation des métriques par la plateforme. Au lot 4, E1 **classe et enregistre** ;
  l'extraction des contenus N1 attend le lot 5, sauf les N3 et N4 actuels.
- Vérification légère E5 : au lot 4, une variante hors bande validée suit le chemin
  normal de son niveau.
- Ordonnanceur, récits et vagues (lot 5).
- Miroir LEGI, EUR-Lex et autres pays ; registre complet avec `controlled_by` ; règle
  « partie prenante » complète ; faits établis (lot 6).
- Autres plateformes (lot 7), médias (lot 8), audit Fable (lot 9), API Batch.

Si cette répartition (en particulier « E1 classe sans extraire les N1 ») te paraît
contraire au plan, pose la question à Jules avant de commencer la phase B.

## 4. Spécifications

### 4.1 Données (une migration `0006`, expand)

- `CREATE EXTENSION IF NOT EXISTS vector`. Si l'extension n'est pas disponible, la
  migration doit échouer avec un message clair.
- Nouvelles tables :
  - `content_embeddings` : partitionnée par semaine, sans index vectoriel ; partitions
    créées comme celles de `observations` et purgées après 14 jours ;
  - `claim_group_events`, `reference_facts`, `verdict_bands`, `processing_profiles`,
    `level_overrides`, `router_models`.
- Colonnes ajoutées (nullables) :
  - `claims.embedding halfvec(512)` (non indexé) et `claims.embedding_model` ;
  - `claim_groups.representative_embedding halfvec(512)` (index HNSW
    `halfvec_cosine_ops`), `reference_fact_id`, `band` ;
  - `verdicts.reused_from_verdict_id`, `verdicts.reuse_rule`.
- Toute `CHECK` existante à modifier est élargie, jamais renommée ni supprimée.
- Statuts en `TEXT` + `CHECK`, IDs en chaînes dans l'API. Downgrade propre.
- Le schéma doit rester compatible avec le code du lot 3 déjà déployé : le pre-deploy
  tourne avant le nouveau code.

### 4.2 Domaine pur (`core/domain/factcheck/`, bibliothèque standard)

- Comparaison de signatures normalisées (Q9) :
  - égalité stricte ;
  - compatibilité de période (« actuellement » résolu à la date du post) ;
  - unités et échelles ;
  - polarité ;
  - variante de définition.
- Bandes de valeurs :
  - tolérance τ ;
  - bandes « contient », « < 1 τ », « < 3 τ » et « au-delà », chacune avec son côté ;
  - intervalles ouverts (« plus de 10 000 ») ;
  - détection d'`outdated` par la fiche précédente.
- Décision de réutilisation : directe, par bande, ou aucune, selon le niveau (jamais
  directe en N4), la péremption et le régime de la note (une note provisoire n'est
  jamais héritée ; une note de régime « objet » ou « formulation » suit les règles de
  `contexte.md`).
- Péremption par type d'affirmation (valeurs en configuration).
- Régression logistique E1 en Python pur (512 poids, aucun numpy), seuils par niveau.
- Profils : schéma des réglages, bornes de sécurité (seuil de cache ≥ 0,85, budget
  unitaire ≤ budget mensuel…), comparaison de versions.
- Similarité cosinus et choix du médoïde sur de petits ensembles, en Python pur.

Tests unitaires exhaustifs, avec les paires pièges : même phrase avec une valeur
différente, une négation, une autre période, un autre territoire, une autre définition.

### 4.3 Prompts et schémas

- Tu écris toi-même le prompt `pair_arbiter` (anglais,
  `src/tower/services/factcheck/prompts/`) et son schéma de sortie strict. Il répond à
  « les deux affirmations disent-elles la même chose, vérifiables par les mêmes
  preuves ? », avec une raison fermée : même affirmation, valeur différente, période
  différente, territoire différent, définition différente, polarité opposée, sujet
  différent. Il reçoit les deux **textes d'origine**, chacun dans sa langue, sans
  l'auteur, et il est conçu pour ne jamais fusionner à tort (dans le doute : différent).
- Relis les prompts du lot 3 à la lumière de la validation. Ne les modifie que si la
  session de validation l'a demandé, et toute modification de prompt passe par
  `tower factcheck eval` avant et après.
- Le test « aucun champ interdit » couvre aussi `pair_arbiter`.

### 4.4 Services et jobs

- **Service `factcheck`** :
  - abonnement `content.ingested` → job `factcheck.embed_batch` (file `fc-bulk`, lots
    de 100 à 2 000 textes, clé de dédoublonnage par lot) → E1 ;
  - E3 inséré entre E2 et la création de l'enquête : `record_extraction` rattache
    l'affirmation à un groupe existant ou en crée un ;
  - réutilisation au lieu d'une enquête quand une règle s'applique ;
  - job `factcheck.revalidate` ;
  - tâche périodique de péremption ;
  - partitions de `content_embeddings`.
- Tout passe par des cas d'usage, avec l'événement dans la même transaction, et des
  abonnements courts.
- **Événements** à ajouter à `docs/events.md` :
  - `claim_group.created`, `claim_group.merged`, `claim_group.split` ;
  - `claim_group.level_changed` ;
  - `verdict.reused` ;
  - `verdict.expired` ;
  - `reference_fact.validated`, `verdict_band.validated` ;
  - `processing_profile.activated`, `level_override.created`.
- **Budgets** : portées `level:N0..N4` créées par `tower llm seed` avec des plafonds par
  défaut en configuration ; embeddings imputés au niveau du contenu.

### 4.5 Gate et bot

- Endpoints : les groupes et leurs membres, la fusion et la scission (`editor`), les
  fiches de référence (création `reviewer`, validation `editor`), les bandes (`editor`),
  les profils (lecture `reviewer`, activation et retour arrière `admin`), et les
  forçages de niveau (`editor`).
- Le brouillon affiché dans Discord indique :
  - les autres posts et plateformes du groupe ;
  - le verdict réutilisable s'il y en a un ;
  - la fiche et la bande quand l'affirmation est chiffrée.
- Les forçages et les profils sont journalisés, avec auteur et motif.
- Les catalogues `fr` et `en` sont complétés, avec le test de parité.

### 4.6 Évaluation

- Format JSONL complété : paires pièges avec groupe attendu (« même », « différent »),
  paires interlingues, fiche et bande attendues.
- Nouvelles métriques :
  - fausse réutilisation ;
  - fusion à tort et scission manquée ;
  - pureté des groupes ;
  - part des affirmations tranchées sans recherche payante.
- Quelques cas d'exemple dans `tests/fixtures/`, marqués comme exemples.

### 4.7 Codes, configuration, documentation

- Codes `TWR-FCK-*` à la suite des existants, sans renuméroter ; régénère
  `docs/errors.md`.
- Variables (péremption par type, seuils E1 par défaut, τ et planchers, plafonds par
  niveau, liste des indicateurs INSEE suivis) dans `docs/configuration.md` et
  `.env.example`.
- Documentation :
  - mises à jour de `docs/fact-check.md`, `api.md`, `events.md`, `database.md`,
    `auth.md`, `intercom.md`, `services.md`, `architecture.md`, `roadmap.md`,
    `factcheck/feuille-de-route.md` et `decisions.md` ;
  - résumé de session `docs/sessions/AAAA-MM-JJ-lot-4-v1.md`.

## 5. Exigences transversales

- **RAM** :
  - aucun numpy, ni pandas, ni modèle local ; vecteurs manipulés en `array('f')` ou en
    listes, calculs lourds dans PostgreSQL ;
  - tests d'isolation : gate ne charge ni SDK ni adaptateur, et un hangar sans
    `factcheck` ne charge rien du fact-check ;
  - mesure du RSS au repos et après un lot d'embeddings et une enquête, pour gate,
    hangar (5 services) et intercom, avant et après ce lot, avec la méthode du lot 3.
    Cibles : gate au plus +5 Mo, hangar au plus +20 Mo au repos (rapporte aussi la
    valeur après traitement), intercom au plus +10 Mo.
- **Base** :
  - mesure la taille de l'index HNSW et le temps de requête sur un jeu synthétique
    réaliste (par exemple 50 000 groupes) ;
  - vérifie `maintenance_work_mem` pendant la construction ;
  - pas d'index vectoriel ailleurs que sur les groupes.
- **Économie d'argent** :
  - embeddings par lots, en cache, et seulement pour les langues traitées ;
  - arbitre seulement en zone grise ou pour une paire interlingue ;
  - la réutilisation doit faire baisser le nombre d'enquêtes, à mesurer.
- **Sûreté de la réutilisation** : en cas de doute, ne pas réutiliser. Chaque
  réutilisation est tracée (règle, verdict d'origine) et réversible par une scission.
- **Égalité de traitement** : ni le regroupement ni la réutilisation ne dépendent de
  l'auteur.
- **Socle** :
  - un cas d'usage pour toute écriture ;
  - événements dans la même transaction ;
  - services qui n'importent que `tower.core` ;
  - abonnements courts ;
  - migrations expand/contract.
- **Code en anglais, documentation en français.**

## 6. Méthode de travail

**Branche** : pars de `main`, sur la branche indiquée par ta session. Pas de pull
request sans demande explicite de Jules.

**Base de test** : PostgreSQL 16 réel avec pgvector (voir `docs/development.md` ; tests
sur `postgresql://postgres@localhost:5433/tower_test`). Installe-les s'ils manquent. Ne
simule jamais la base. Donne une base par sous-agent (`tower_test_g1`…) pour éviter les
troncatures croisées.

**Phase A — Contrats et prompt (toi seul)**
1. Vérifie que la suite existante passe (1092 tests à la fin du lot 3, plus ceux de la
   session de validation).
2. Vérifie la disponibilité de pgvector sur Railway (documentation officielle).
3. Écris les contrats :
   - modèles SQLAlchemy et migration `0006` ;
   - valeurs et signatures du domaine ;
   - schémas ;
   - noms des jobs, topics, codes, variables et permissions ;
   - les nouvelles tables dans la liste `TABLES` de `tests/conftest.py`.
4. Écris le prompt `pair_arbiter`.
5. Vérifie `alembic check`, `ruff` et `mypy`, puis commite. Ne commite jamais en
   `git commit -a` pendant que des sous-agents travaillent : ajoute les fichiers un par
   un.

**Phase B — Sous-agents Claude Sonnet 5.5** (`model: "sonnet"` ; vérifie dans le premier
rapport que c'est bien Sonnet 5.5).

Briefs autonomes, dans des fichiers du répertoire de travail temporaire. Mets les
règles communes dans un fichier partagé et le contrat de données (formes JSON, flux)
dans un autre, et ne répète pas leur contenu dans chaque brief. Chaque brief contient :
- les fichiers autorisés, et seulement ceux-là ;
- les signatures exactes ;
- le comportement et les cas limites ;
- les tests à écrire ;
- les commandes ;
- les interdits ;
- un rapport de 10 lignes au plus, qui commence par le modèle.

| Agent | Contenu | Dépend de |
|---|---|---|
| G1 | Domaine pur (4.2) + tests | contrats |
| G2 | Embeddings (port, adaptateur, doublure factice), E1, `content_embeddings` et partitions, `train-router` + tests | contrats |
| G3 | E3 (recherche pgvector, signatures, arbitre, fact-checks), groupes, fusion et scission, réutilisation dans `record_extraction` + tests | G1 (ou contrats) |
| G4 | Fiches de référence, bandes, péremption et revalidation, alimentation INSEE + tests | G1 |
| G5 | Gate et bot (groupes, fiches, bandes, profils, forçages, brouillon enrichi, lecture publique des variantes, i18n) + profils et niveaux (cas d'usage, bornes, budgets) + tests | contrats |
| G6 | Évaluation étendue (4.6) + tests | G3, G4 |

Lance G1, G2 et G5 ensemble, puis G3 et G4, puis G6. Pendant ce temps, rédige la
documentation. Relis les diffs et lance les tests de chaque agent avant de commiter ses
fichiers. Corrige toi-même les petits écarts ; envoie un brief court pour le reste.

**Phase C — Intégration (toi)**
1. Suite complète : ruff, mypy strict, pytest, `alembic check`, migrations aller et
   retour.
2. Smoke test des vrais processus (`TOWER_LLM_FAKE=1` si pas de clés, gate en
   `TOWER_HOST=0.0.0.0`, `curl --noproxy '*'`, faux serveur X local comme au lot 3) :
   - deux comptes suivis publient des variantes d'une même affirmation (même valeur,
     valeur voisine, autre période, autre langue) → un groupe, une seule enquête,
     `verdict.reused` pour la variante identique ;
   - la variante chiffrée hors bande n'hérite pas ; une fois la bande validée par le
     bot, une nouvelle variante dans cette bande hérite ;
   - scission depuis l'API → le verdict n'est plus hérité ;
   - forçage de niveau et changement de profil depuis le bot (script contre le vrai
     gate, sans Discord) ;
   - lecture publique d'une variante couverte ;
   - `tower factcheck eval` sur les exemples, rejoué à coût nul.
3. Mesures RSS et taille de l'index, tests d'isolation.
4. Documentation cohérente, `docs/errors.md` régénéré, résumé de session (résultats
   réels, échecs, ce qui n'a pas été vérifié), décisions, commits en anglais, push.

Note du lot 3 : l'IP de sortie partagée de l'environnement cloud peut être limitée par
Wikimédia (429 ou 403). N'insiste pas : note-le et continue.

## 7. Économie d'usage (forfait Claude de Jules)

Chaque token compte. Sois **efficace et économe, sans rien céder sur la qualité** :

- **Répartition** :
  - toi (Opus 5.5), tu conçois, écris le prompt, relis et intègres ;
  - les sous-agents Sonnet 5.5 implémentent ;
  - aucun sous-agent Opus, aucun sous-agent d'exploration.
- **Lecture ciblée** : seulement les sections utiles ; ne relis pas ce que tu viens
  d'écrire ; dans les briefs, des chemins plutôt que des copies de fichiers.
- **Briefs** précis pour éviter les allers-retours, rapports de 10 lignes au plus, le
  moins de sous-agents possible (les six du tableau), corrections regroupées.
- **Outils** : appels indépendants regroupés dans un même tour ; sorties courtes
  (`pytest -q`, `| tail -n 30`) ; suite complète aux points de contrôle seulement ; pas
  de boucle d'attente sur un service externe qui refuse.
- **Recherche web** limitée à la documentation officielle (Railway pour pgvector,
  OpenAI pour les embeddings, pgvector).
- **Pas de réécriture complète**, pas de documentation en double, rien hors périmètre.
- **Messages à Jules courts**, un point par phase.

## 8. Rapport final attendu

En français, court :
- ce qui est livré ;
- une estimation de l'usage (sous-agents, allers-retours) ;
- les vérifications avec leurs chiffres : tests, RSS avant et après, taille et latence
  de l'index, fausse réutilisation sur les exemples ;
- ce qui n'a pas pu être vérifié (appels réels, qualité du regroupement sur un vrai
  corpus) ;
- les écarts au plan ;
- ce qu'il faut à Jules pour la suite : paires pièges et paires interlingues du jeu de
  référence, liste des indicateurs suivis, plafonds par niveau, premier entraînement du
  classifieur une fois des données réelles disponibles ;
- la prochaine étape proposée : le lot 5 (flux de masse, ordonnanceur, vagues).
