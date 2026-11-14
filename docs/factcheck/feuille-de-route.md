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

**Livré le 2026-11-11** ([../llm.md](../llm.md)). Écarts : rôle factice testé avec des
fournisseurs factices (aucune clé disponible : l'appel réel aux deux fournisseurs reste
à faire avec les clés) ; `BatchLLMClient`, `KnowledgeSource` et `SearchClient` livrés
comme contrats seulement ; codes `TWR-WEB-*` ajoutés pour la récupération web.

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

**Livré le 2026-11-11, en partie** ([../ingestion.md](../ingestion.md)) : tables,
E0 (normalisation, empreintes, SimHash par bandes, langue), endpoint et configuration
de l'extension, liste de suivi, service `ingest` (copies, partitions, secrets),
`media_assets` avec colonnes d'empreinte perceptuelle prêtes. **Reportés** (hors
périmètre de la partie 1) : `radar` et appels à l'API X (**livrés en partie 2**, en
service hangar, voir [../radar.md](../radar.md)), calcul de
l'empreinte perceptuelle, registres par pays, test de charge à 20 000 contenus par
jour et mesure du rappel des quasi-copies sur un corpus. Point à trancher : une
substitution d'un mot dans un texte de 40 mots donne une distance SimHash de 4 à 7,
au-delà du seuil de 3 (voir le résumé de session).

- Tables `entities`, `platform_accounts`, `watched_accounts`, `content_items` (avec
  `x_post_id` généré), `content_snapshots`, `observations`, `observation_counters`,
  `installation_daily` (pseudonyme du jour, secret détruit chaque jour : aucun
  profilage possible).
- Service `ingest`, endpoint de l'extension (déjà prévu par la feuille de route du
  socle), processus `radar` pour X.
- E0 : normalisation, empreinte exacte, SimHash par bandes, détection de langue,
  liste des langues traitées en configuration.
- Empreinte perceptuelle des images **dès l'ingestion** (nécessaire aux notes de
  contexte rattachées à un média), même si la lecture d'images vient au lot 8.
- `observations` partitionnée ; `content_items` non partitionnée, clé interne
  `BIGINT` référencée partout.
- Registres par pays (`country_profiles`, `official_domains`, `fact_checkers`) amorcés
  ([international.md](international.md)).

Critère : 20 000 contenus simulés par jour ingérés sans retard ; quasi-copies
détectées (rappel ≥ 95 % sur un corpus de test) ; aucune donnée sur l'utilisateur de
l'extension.

## Lot 3 — MVP : comptes ciblés (N3), verdict et contexte

> **Livré en développement le 2026-11-11** (sans clé d'API : modèles et sources à clé
> remplacés par des doublures ; Wikipédia et Wikidata appelées pour de vrai au smoke
> test). Implémentation : [../fact-check.md](../fact-check.md). **Réglage à faire** :
> session de validation avec les clés et au moins 50 affirmations du jeu de référence
> (`tower factcheck eval`). Ajouts au périmètre initial : demandes manuelles N4
> (`/factcheck`), lecture publique du verdict validé, traductions à la validation.

- E2 (extraction temps réel), E6 (cadrage), E7 (collecte) avec un Atlas minimal :
  Google Fact Check Tools, Wikidata, Wikipédia (révision figée + revert risk),
  API Légifrance (PISTE), INSEE BDM, Brave Search.
- E8 : enquête standard (juge Sonnet, 4 recherches, second avis aveugle OpenAI),
  escalade en profondeur renforcée (Opus) sur désaccord de fond, sources
  contradictoires ou confiance < 0,6.
- E9 : brouillon **et note de contexte** dès le MVP (demande de Jules), contrôles
  déterministes.
- E10 : confiance par régression logistique à coefficients posés à la main,
  revérification, ouverture de dossier, `draft.ready`.
- E11 : revue dans le bot Discord (dépend du lot intercom du socle), capture des
  corrections, journal de coûts.
- Ni embeddings, ni cache de verdicts, ni récits à ce stade.

Critère : sur le jeu de référence, taux de faux confiants ≤ 2 %, 100 % de citations
valides, coût moyen ≤ 0,25 $ par enquête N3 ; en production, au moins 10 brouillons
relus avec temps de relecture médian ≤ 10 min.

## Lot 4 — V1 : embeddings, groupes, cache, profils

> **Livré en développement le 2026-11-12** (sans clé d'API : embeddings et arbitre
> remplacés par des doublures). Implémentation :
> [../fact-check.md](../fact-check.md#lot-4--v1--embeddings-groupes-réutilisation-profils).
> Écarts : E1 classe et enregistre sans déclencher l'extraction des N1 (lot 5) ; une
> variante chiffrée de valeur différente forme son propre groupe relié à la fiche ;
> pgvector impose le template PostgreSQL de Railway avec pgvector. Mesures sur exemples
> seulement (fausse réutilisation 0 % sur 2 paires pièges d'exemple) ; la mesure sur
> les 50 paires pièges du jeu de référence reste à faire.

- E1 (embeddings, classifieur en règle permissive), E3 (appariement, signature,
  arbitre), groupes, fusions et scissions réversibles.
- Fiches de référence, cache de verdicts avec péremption par type, revalidation.
- `processing_profiles` et `level_overrides` versionnés, modifiables depuis Discord.
- Niveaux N0 à N4 et budgets par niveau.

Critère : fausse réutilisation < 1 % sur les paires pièges ; ≥ 50 % des affirmations
N1/N2 tranchées sans recherche payante ; profils modifiables à chaud avec bornes de
sécurité.

## Lot 5 — V2 : flux de masse, ordonnanceur, vagues

> **Livré en développement le 2026-11-12** (sans clé d'API : modèles, embeddings et
> API X remplacés par des doublures). Implémentation :
> [../fact-check.md](../fact-check.md#lot-5--v2--flux-de-masse-ordonnanceur-récits-et-vagues).
> Écarts : API Batch reportée (micro-lots synchrones) ; voie rapide étendue aux N3/N4
> (sinon une vague retardait les N3) ; les signaux de coordination restent informatifs.
> Test de charge (100 000 posts en 1 h, soit 24 h accélérées 24 fois, un réplica
> hangar) : délai N3 jusqu'au brouillon p50 5,9 s et p95 21,3 s, première enquête de
> vague assemblée 1,4 s après la bascule en surge, aucun budget dépassé. **Critère de
> coût non atteint sous doublures** : 0,0000155 $ par post pour la masse (N1), 0,0000208 $
> tout compris, car l'allocation gratuite s'épuise et le modèle payant prend le relais.
> Rattachement aux récits et échantillonnage non exercés (les doublures ne produisent ni
> entités ni similarités réalistes). Détails :
> [../sessions/2026-11-12-lot-5-v2.md](../sessions/2026-11-12-lot-5-v2.md).

- Flux de l'extension en N1, micro-lots E2 (avec test de contamination), API Batch.
- E4 ordonnanceur (singleton), promotion et rétrogradation, quota de brouillons.
- E5 vérification légère.
- **Récits et mode vague** ([vagues.md](vagues.md)) : rattachement incrémental,
  détection par part du flux (écart de type Poisson, seuils absolus puis quantiles),
  dossier de preuves partagé, nouveautés qui prennent, voie rapide, compteurs d'observations,
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
- Classifieur E1 distillé, recalibrage de la confiance (ordonnée et 3 ou 4
  coefficients à 200 revues, modèle complet à 100 refus), bandes de valeurs validées
  par les relecteurs, niveau N4 complet.

Critère : ≥ 70 % des affirmations tranchées sans recherche web payante ; calibration
avec écart moyen < 0,1 ; distillation qui réduit de moitié les appels E2 sans perte de
rappel au-delà de 2 points.

**État (2026-11-14)** : livré en développement (voir
[../fact-check.md](../fact-check.md#lot-6--v3--atlas-complet-et-qualité)) : Eurostat,
Banque mondiale, OCDE, FMI, miroir LEGI des 9 textes prioritaires, EUR-Lex, Fedlex, Lois
du Canada, index d'actualité et GDELT, registre éditable avec `controlled_by` et règle
partie prenante, registres par pays, recalibrage et distillation avec leurs garde-fous,
revalidation continue du N4. Non faits : ONU (indicateurs ODD), passage en couverture A,
nouvelles langues (ils exigent un sous-ensemble de référence étiqueté), bandes de valeurs
validées par les relecteurs au-delà du lot 4. Critères non mesurables sous doublures :
l'outillage est en place (`tower factcheck eval`, `calibrate`, `train-router`).

## Lot 7 — Multi-plateforme

> **Reporté** (Jules, 2026-11-12) : pas de nouvelle plateforme pour l'instant.

- Adaptateur et ingestion Bluesky (Jetstream), puis Threads et Mastodon.
- Comptes multi-plateformes par entité.

Critère : une vague présente sur X et Bluesky est reconnue comme un seul récit.

## Lot 8 — Médias

> **Reporté** (Jules, 2026-11-12) : ni image ni vidéo pour l'instant.

- `media_assets`, service `media` : M0 (empreintes, images clés), M1 (OCR,
  transcription), M2 (provenance, première apparition).
- Corpus de test d'images recyclées.

Critère : ≥ 90 % des images recyclées du corpus datées correctement ; RSS du réplica
`media` mesuré et borné.

## Lot 9 — V4 : audit et neutralité

> Traité comme un **audit** (Jules, 2026-11-12), après le passage au réel.

- Audit hebdomadaire Fable, tableau de bord de neutralité (quatre mesures), exemples
  dynamiques dans les prompts ; brouillons pièges seulement si le taux d'acceptation
  sans modification le justifie et avec l'accord de l'équipe.

Critère : rapport de neutralité mensuel publié en interne ; écart nul au test de
permutation.

## Dépendances avec la feuille de route du socle

Le lot 2 correspond à l'étape « Ingestion » de [../roadmap.md](../roadmap.md) ; le
lot 3 dépend de `radar` (lecture des comptes suivis) et d'`intercom` (bot Discord),
**livrés par la partie 2 le 2026-11-11** ([../radar.md](../radar.md),
[../intercom.md](../intercom.md)), testés sans appel réel à X ni à Discord.
Les lots 1 et 2 peuvent avancer en parallèle de la phase 0.
