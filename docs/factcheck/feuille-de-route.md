# Feuille de route du fact-check

Ordre de livraison, en lots livrables indépendamment, chacun avec un critère chiffré
de passage. Chaque lot respecte la définition de « terminé » de `CLAUDE.md` (tests,
migration relue, documentation, résumé de session). L'ordre suit le plan de Fable (le
MVP sert d'abord les comptes ciblés, où chaque post a le plus de valeur) et intègre
les demandes de Jules.

## Phase 0 — Avant tout code de jugement

| Tâche | Responsable | Critère |
|---|---|---|
| Constituer le jeu de référence de 300 affirmations ([evaluation.md](evaluation.md)) | Équipe (aide possible d'un modèle pour la collecte, validation humaine) | 300 cas étiquetés et relus |
| Confirmer les volumes réels et la capacité de relecture | Jules | Valeurs remplacées dans [couts.md](couts.md) |
| OpenAI : créer l'organisation dédiée, activer le partage, relever le palier d'usage et la **liste exacte des modèles éligibles** dans le tableau de bord | Jules | Liste reportée dans `llm_models` |
| Comptes et clés : Anthropic, OpenAI, PISTE (Légifrance), INSEE, Google Cloud (Fact Check Tools), Brave Search | Jules | Clés dans les variables Railway |
| Vérifier pgvector ≥ 0,8 sur le PostgreSQL de Railway | Claude | Extension créée en environnement de test |
| Validation juridique : partage des données avec OpenAI, étiquetage politique (RGPD art. 9), conservation des pages | Jules (+ conseil) | Décision écrite dans `decisions.md` |

## Lot 1 — Fondations communes

Ports et briques réutilisés par tous les étages, sans encore de fact-check.

- Ports étendus : `LLMClient`, `BatchLLMClient`, `Embedder`, `KnowledgeSource`,
  `SearchClient`, `WebFetcher`, `PlatformClient` ([pipeline.md](pipeline.md)).
- Adaptateurs OpenAI et Anthropic (SDK officiels, import paresseux), doublures de test.
- Routeur de modèles : `llm_models`, `llm_routes`, `llm_calls` (cache de contenu),
  `llm_free_usage`, `budget_counters` (réserver puis régler).
- Registre des prompts (fichiers versionnés par empreinte).
- Codes d'erreur `TWR-LLM-*`, topics `budget.*`, CLI `tower llm usage`.

Critère : un rôle factice appelle les deux fournisseurs avec sortie structurée ; le
rejeu sert depuis le cache sans appel ; la réservation de budget reste exacte avec
deux réplicas concurrents (test réel sur PostgreSQL) ; RSS mesuré.

## Lot 2 — Ingestion générique

- Tables `entities`, `platform_accounts`, `watched_accounts`, `content_items` (avec
  `x_post_id` généré), `content_snapshots`, `observations`, `observation_counters`,
  `installations`.
- Service `ingest`, endpoint de l'extension (déjà prévu par la feuille de route du
  socle), processus `radar` pour X.
- E0 : normalisation, empreinte exacte, SimHash par bandes, détection de langue,
  liste des langues traitées en configuration.
- Registres par pays (`country_profiles`, `official_domains`, `fact_checkers`) amorcés
  ([international.md](international.md)).

Critère : 20 000 contenus simulés par jour ingérés sans retard ; quasi-copies
détectées (rappel ≥ 95 % sur un corpus de test) ; aucune donnée sur l'utilisateur de
l'extension.

## Lot 3 — MVP : comptes ciblés (N3), verdict et contexte

- E2 (extraction temps réel), E6 (cadrage), E7 (collecte) avec un Atlas minimal :
  Google Fact Check Tools, Wikidata, Wikipédia (révision figée + revert risk),
  API Légifrance (PISTE), INSEE BDM, Brave Search.
- E8 : juge Sonnet (Opus en option), contradicteur OpenAI.
- E9 : brouillon **et note de contexte** dès le MVP (demande de Jules), contrôles
  déterministes.
- E10 : confiance par grille fixe, revérification, ouverture de dossier, `draft.ready`.
- E11 : revue dans le bot Discord (dépend du lot intercom du socle), capture des
  corrections, journal de coûts.
- Ni embeddings, ni cache de verdicts, ni récits à ce stade.

Critère : sur le jeu de référence, taux de faux confiants ≤ 2 %, 100 % de citations
valides, coût moyen ≤ 0,25 $ par enquête N3 ; en production, au moins 10 brouillons
relus avec temps de relecture médian ≤ 10 min.

## Lot 4 — V1 : embeddings, groupes, cache, profils

- E1 (embeddings, classifieur en règle permissive), E3 (appariement, signature,
  arbitre), groupes, fusions et scissions réversibles.
- Fiches de référence, cache de verdicts avec péremption par type, revalidation.
- `processing_profiles` et `level_overrides` versionnés, modifiables depuis Discord.
- Niveaux N0 à N4 et budgets par niveau.

Critère : fausse réutilisation < 1 % sur les paires pièges ; ≥ 50 % des affirmations
N1/N2 tranchées sans recherche payante ; profils modifiables à chaud avec bornes de
sécurité.

## Lot 5 — V2 : flux de masse, ordonnanceur, vagues

- Flux de l'extension en N1, micro-lots E2 (avec test de contamination), API Batch.
- E4 ordonnanceur (singleton), promotion et rétrogradation, quota de brouillons.
- E5 vérification légère.
- **Récits et mode vague** ([vagues.md](vagues.md)) : rattachement incrémental,
  détection par z-score robuste, voie rapide, compteurs d'observations,
  échantillonnage sous charge, signaux de coordination.

Critère : masse sous 0,00001 $ par post ; test de charge d'une vague simulée
(100 000 posts en 24 h sur un récit) sans retard des files N3 ni dépassement de
budget, premier brouillon en moins de 2 h.

## Lot 6 — V3 : Atlas complet et qualité

- Atlas : Eurostat et autres API statistiques, miroir LEGI des codes prioritaires,
  EUR-Lex, Fedlex, lois du Canada, index d'actualité (RSS), GDELT, registre de
  fiabilité éditable.
- International : Banque mondiale, FMI, OCDE et ONU ; registres par pays complets
  (`jurisdiction_sources`, `news_feeds`, `election_calendar`) ; passage en couverture A
  des pays les plus présents dans le flux ; nouvelles langues activées chacune après
  son sous-ensemble de référence.
- Classifieur E1 distillé, confiance calibrée par régression, juge Opus en N3,
  seconde passe du juge, niveau N4 complet.

Critère : ≥ 70 % des affirmations tranchées sans recherche web payante ; calibration
avec écart moyen < 0,1 ; distillation qui réduit de moitié les appels E2 sans perte de
rappel au-delà de 2 points.

## Lot 7 — Multi-plateforme

- Adaptateur et ingestion Bluesky (Jetstream), puis Threads et Mastodon.
- Comptes multi-plateformes par entité.

Critère : une vague présente sur X et Bluesky est reconnue comme un seul récit.

## Lot 8 — Médias

- `media_assets`, service `media` : M0 (empreintes, images clés), M1 (OCR,
  transcription), M2 (provenance, première apparition).
- Corpus de test d'images recyclées.

Critère : ≥ 90 % des images recyclées du corpus datées correctement ; RSS du réplica
`media` mesuré et borné.

## Lot 9 — V4 : audit et neutralité

- Audit hebdomadaire Fable, tableau de bord de neutralité (quatre mesures), exemples
  dynamiques dans les prompts, brouillons pièges pour mesurer la vigilance.

Critère : rapport de neutralité mensuel publié en interne ; écart nul au test de
permutation.

## Dépendances avec la feuille de route du socle

Le lot 2 correspond à l'étape « Ingestion » de [../roadmap.md](../roadmap.md) ; le
lot 3 dépend de `radar` (lecture des comptes suivis) et d'`intercom` (bot Discord).
Les lots 1 et 2 peuvent avancer en parallèle de la phase 0.
