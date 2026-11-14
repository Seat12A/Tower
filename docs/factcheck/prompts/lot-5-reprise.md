# Prompt de reprise : lot 5 (V2 du fact-check)

À coller dans une nouvelle session Claude Code (Opus) sur le dépôt `Seat12A/Tower`.

---

Tu reprends l'implémentation du **lot 5 : V2 du fact-check** (flux de masse N1,
micro-lots E2, ordonnanceur E4, vérification légère E5, récits et vagues) du dépôt 12A
Tower. Une session précédente a livré le lot 4 et commencé le lot 5, puis s'est arrêtée
(limite d'usage). Travaille sur la branche `claude/friendly-ptolemy-kndvzh` (pars de son
dernier commit, pas de `main`). Jules peut être absent : prends les décisions toi-même,
note-les dans `docs/decisions.md`, et sois économe en tokens (sous-agents Claude Sonnet
seulement pour l'implémentation, briefs précis, sorties de commandes courtes).

## Rôle et économie d'usage (forfait Claude de Jules)

- **Toi (Opus)** : tu conçois, relis, intègres et vérifies. **Les sous-agents Claude
  Sonnet** (`model: "sonnet"`, vérifie dans leur premier rapport que c'est bien Sonnet)
  implémentent. Aucun sous-agent Opus, aucun sous-agent d'exploration.
- Briefs autonomes et précis (fichiers autorisés seulement, signatures, cas limites,
  tests, commandes, interdits), rapport de 10 lignes au plus qui commence par le modèle.
  Les briefs du lot 5 existent déjà : réutilise-les, ne les réécris pas.
- Le moins de sous-agents possible, lancés en parallèle quand leurs fichiers sont
  disjoints ; une base de test par sous-agent. Ne commite jamais en `git commit -a`
  pendant que des sous-agents travaillent : ajoute les fichiers un par un.
- Lecture ciblée (sections utiles seulement), chemins plutôt que copies, sorties de
  commandes courtes (`pytest -q`, `| tail`), suite complète aux points de contrôle
  seulement, appels d'outils indépendants regroupés, recherche web limitée à la
  documentation officielle, pas de réécriture complète ni de documentation en double.
- Messages à Jules courts, un point par phase.

## Exigences transversales (non négociables, détail dans `CLAUDE.md`)

- **RAM** (Railway facture environ 10 $ par Go et par mois) : imports paresseux, aucun
  numpy, pandas ni modèle local, vecteurs en listes, calculs lourds dans PostgreSQL,
  tout cache ou tampon borné, processus sans état. Mesure du RSS avant et après le lot
  (méthode du lot 4 : processus lancé directement, mesure à 8 s, « avant » dans un
  worktree) ; cibles : gate au plus +5 Mo, hangar au plus +20 Mo au repos (rapporte aussi
  la valeur après traitement), intercom au plus +10 Mo. Tests d'isolation des imports :
  gate ne charge ni SDK ni adaptateur, un hangar sans `factcheck` ne charge rien du
  fact-check.
- **Coût minimal** : masse sous 0,00001 $ par post, lots, cache de contenu des appels,
  pas d'appel de modèle évitable, budgets par niveau respectés.
- **Scalabilité** : réplicas sans état, PostgreSQL seule infrastructure (outbox,
  `SKIP LOCKED`, verrous consultatifs), contre-pression sur les files.
- **Socle** : toute écriture métier par un cas d'usage avec permission, audit et
  événement dans la même transaction ; abonnements courts (ils enfilent des jobs) ;
  handlers idempotents ; services qui n'importent que `tower.core` ; erreurs `TWR-*`
  (jamais renuméroter) et `capture` ; logs JSON courts sur stdout ; migrations
  expand/contract ; IDs en `BIGINT` en base et en chaînes dans l'API.
- **Langues** : tout contenu porte sa langue. Code, commentaires, logs, tests et commits
  en **anglais** ; documentation en **français**.
- **Objectivité et égalité de traitement** sans faux équilibre : ni le regroupement, ni
  les récits, ni la priorité, ni les verdicts ne dépendent de l'auteur ou de son bord ;
  les modèles ne reçoivent jamais l'auteur ni le niveau ; les signaux de coordination
  sont informatifs seulement.
- **Aucune publication automatique, aucun scraping de X.**
- **Sûreté** : dans le doute, ne pas regrouper et ne pas réutiliser un verdict.
- **Base de test réelle** (jamais simulée) ; doublures de modèles `TOWER_LLM_FAKE=1`
  faute de clés, et dis clairement dans le rapport ce qui a été appelé pour de vrai.
- **Définition de « terminé »** (`CLAUDE.md`) : ruff, mypy strict, pytest verts ; tests
  du nominal et des erreurs ; migration relue et `alembic check` ; documentation à jour
  dans le même commit ; `docs/errors.md` régénéré ; résumé de session ; décisions dans
  `docs/decisions.md` ; pas de pull request sans demande explicite.

## Lire d'abord

1. `CLAUDE.md` (règles non négociables).
2. `docs/sessions/2026-11-12-lot-4-v1.md` (état du lot 4, points ouverts) et
   `docs/fact-check.md` (ce qui est implémenté).
3. `docs/factcheck/prompts/lot-5-briefs/CONTRACT5.md` (**contrat et décisions du lot 5,
   déjà arrêtés**) et `COMMON.md` ; puis les briefs `H2.md` à `H6.md`.
4. Le plan : `docs/factcheck/feuille-de-route.md` (lot 5), `docs/factcheck/vagues.md`,
   `docs/factcheck/pipeline.md` (E2, E4, E5), `docs/factcheck/niveaux.md`.

## État exact à la reprise

Fait et commité (phase A du lot 5) :
- migration `0007` (tables `extraction_batches`, `content_extractions`,
  `extraction_audits`, `scheduler_runs`, `narratives`, `narrative_groups`,
  `narrative_events`, `narrative_stats`, `narrative_novelty`, `entity_rarity` ; colonnes
  d'ordonnancement sur `claim_groups`) ; modèles `core/models/factcheck_v2.py` ;
- valeurs, noms (jobs, topics, rôles, clés), codes `TWR-FCK-039..045`, variables
  « Fact-check V2 » de `core/config.py` (pas encore documentées) ;
- schémas, entrées et prompts `extract_batch`, `light_check`, `narrative_title` ; sorties
  factices de ces rôles ; route `extract_batch` ;
- enregistrement du service `factcheck` (`services/factcheck/__init__.py`) vers des
  **modules d'attente** à remplacer : `batching.py`, `scheduler.py`, `light_check.py`,
  `narratives.py` (signatures exactes dans `CONTRACT5.md`).

Travail en cours, **commité tel quel et non vérifié** (commit « WIP »), interrompu au
milieu :
- H2 (micro-lots) : `core/domain/factcheck/batching.py`, `services/factcheck/batching.py`
  (bien avancé), `core/usecases/claims.py` (paramètre `investigate`, marquage
  `content_extractions`) ; tests non écrits ;
- H3 (ordonnanceur, E5) : `core/domain/factcheck/scheduling.py`,
  `core/usecases/scheduling.py`, ajout `skip_encyclopedia` dans `AtlasRequest` ; le
  service `scheduler.py` et `light_check.py` sont encore des modules d'attente ;
- H4 (récits, vagues) : rien d'écrit.
Relis ces fichiers, garde ce qui est juste, complète-les selon les briefs.

## Méthode

1. Vérifie l'environnement : PostgreSQL 16 avec **pgvector ≥ 0.8** sur le port 5433
   (`docs/development.md` : compilation depuis les sources si le paquet Debian est trop
   ancien), bases `tower_test` et une par sous-agent (`tower_test_g1`…). Lance la suite :
   elle doit passer, sauf ce que le WIP casse (à corriger en premier).
2. Termine les briefs H2, H3, H4 (en parallèle, un sous-agent Sonnet chacun, fichiers
   disjoints, comme indiqué dans les briefs), puis H5 (gate et bot des récits, alertes,
   signaux de coordination) et H6 (test de charge, évaluation de contamination).
   Relis chaque diff, lance les tests, commite les fichiers un par un.
3. Intégration :
   - brancher `narratives.waves_step` dans la passe de l'ordonnanceur (déjà appelé par
     import paresseux selon le brief H3) et `filter_for_surges` dans le composeur ;
   - suite complète, ruff, mypy strict, `alembic check`, migrations aller-retour ;
   - smoke test des vrais processus (`TOWER_LLM_FAKE=1`, faux serveur X : outils dans
     `docs/factcheck/prompts/lot-5-briefs/`) : posts N1 via l'extension → micro-lot →
     groupes dormants → ordonnanceur (promotion confirmée, vérification légère) → vague
     simulée (récit, palier, mode vague, quota réservé, fin de vague) sans retard des N3 ;
   - test de charge (`scripts/loadtest_wave.py`, brief H6) : critère du lot 5 (vague de
     100 000 posts en 24 h, en accéléré), coût de la masse < 0,00001 $ par post ;
   - mesures RSS avant/après (méthode du lot 4) ;
   - documentation française à jour (`fact-check.md` section lot 5, `api.md`,
     `events.md`, `database.md`, `configuration.md` et `.env.example` pour les variables
     « Fact-check V2 », `intercom.md`, `services.md`, `roadmap.md`,
     `factcheck/feuille-de-route.md`, `decisions.md`), `docs/errors.md` régénéré ;
   - résumé de session `docs/sessions/AAAA-MM-JJ-lot-5-v2.md` (résultats réels, échecs,
     non vérifié), commits en anglais, push.

## Décisions déjà prises pour le lot 5 (ne pas rouvrir)

- API Batch reportée (l'allocation gratuite couvre le volume de départ).
- Voie rapide des vagues par priorité dans `fc-deep` (pas de nouvelle file).
- Pendant le lot 4 : variantes chiffrées en groupes distincts reliés à la fiche ; arbitre
  hors transaction (une paire interlingue simultanée reste un groupe à part, à
  fusionner) ; voir `docs/decisions.md`, section « Lot 4 ».

## Rappels

- Points ouverts du lot 4 à garder en tête : brouillon assemblé après la fusion de son
  groupe (refusé à la revue, à filtrer dès l'assemblage) ; échec intermittent observé une
  fois sur `tests/test_factcheck_eval.py::test_end_to_end_examples_with_fake_provider_and_cache`
  dans la suite complète (non reproduit isolément).
- À faire par Jules avant tout déploiement : base Railway sur le template pgvector,
  scopes du lot 4 sur le token du bot.
