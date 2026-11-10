# Coûts : argent et RAM

Toutes les estimations reposent sur le scénario de dimensionnement du plan de Fable
(20 000 posts observés par jour, 15 % vérifiables, 3 relecteurs pour environ 12
brouillons relus par jour), à corriger avec les volumes réels. Tarifs relevés le
2026-11-10 ([recherche.md](recherche.md)). Les coûts réels seront mesurés par
`llm_calls` et `budget_counters` dès le premier jour. Recalcul fait après les
arbitrages de Fable ([reponses-fable.md](reponses-fable.md)) : profondeur uniforme avec
escalade, second avis aveugle, lots de 10.

## Hypothèses de tokens

| Appel | Entrée | Sortie | Remarque |
|---|---|---|---|
| Extraction E2, lot de 10 | ~900 d'instructions (en cache) + 10 × 200 | 10 × 120 | ~340 tokens par post |
| Arbitre E3 | ~250 | ~20 | Zone grise et toutes les paires interlingues |
| Vérification légère E5 | ~2 500 | ~300 | Aussi pour les variantes d'un récit |
| Cadrage E6 | ~1 500 | ~1 000 | Sonnet (standard), Opus (renforcé) |
| Juge E8, enquête standard | ~5 000 | ~1 200 | Sonnet ; API Batch (−50 %) en N2, synchrone en N3 |
| Juge E8, profondeur renforcée | ~12 000 | ~2 500 | Opus ; seconde passe seulement sur désaccord |
| Second avis + objection | ~7 000 | ~800 | Grand modèle OpenAI gratuit |
| Rédaction E9 | ~3 000 | ~600 | Sonnet |
| Embeddings | posts ~80, passages ~160 | — | Seuls les 50 meilleurs passages d'une enquête sont vectorisés |

## Coût unitaire d'une enquête

| Élément | Coût |
|---|---|
| Cadrage Sonnet | 0,013 $ |
| Juge Sonnet | 0,011 $ (lot, N2) ou 0,022 $ (N3) |
| Rédaction Sonnet | 0,012 $ |
| 4 recherches web | 0,020 $ (moins si l'Atlas tranche) |
| 1 lecture API X | 0,005 $ |
| Second avis | 0 $ (allocation gratuite) |
| **Enquête standard** | **~0,06 $ (N2), ~0,07 $ (N3)** |
| Supplément de profondeur renforcée : cadrage Opus 0,026 $ + juge Opus 0,098 $ + 4 recherches 0,020 $ + seconde passe Opus sur désaccord (~1 cas sur 2) 0,049 $ | **+~0,19 $** |
| **Enquête N4** (toujours renforcée, plus de recherches) | **~0,32 $** |

N1 : environ 0,000005 $ par post (embedding ; extraction gratuite ; part de
vérification légère).

## Projection mensuelle (régime établi)

Le taux d'escalade (part des enquêtes standard qui passent en profondeur renforcée)
n'est pas connu avant la mise en service. Trois hypothèses :

| Poste | Volume mensuel | Escalade 10 % | Escalade 25 % | Escalade 40 % |
|---|---|---|---|---|
| Petits modèles OpenAI | ~66 M de tokens | 0 $ | 0 $ | 0 $ |
| Second avis et objections OpenAI | ~5 M de tokens | 0 $ | 0 $ | 0 $ |
| Repli payant au palier bas | marge faible sur 2,5 M/jour | ~1 $ | ~1 $ | ~1 $ |
| Embeddings | ~40 M de tokens | ~0,8 $ | ~0,8 $ | ~0,8 $ |
| Recherches web de E5 (N1) | ~1 200 requêtes | ~1 $ | ~1 $ | ~1 $ |
| Enquêtes N2 | 300 | ~24 $ | ~33 $ | ~41 $ |
| Enquêtes N3 | 180 | ~16 $ | ~22 $ | ~27 $ |
| Enquêtes N4 et vagues | 30 | ~10 $ | ~10 $ | ~10 $ |
| Audit hebdomadaire Fable 5.2 (lot) | 4 lots | ~10 $ | ~10 $ | ~10 $ |
| **Total IA et recherche** | | **~63 $** | **~77 $** | **~91 $** |
| API X (comptes suivis, confirmations, revérifications) | ~10 000 lectures | ~50 $ | ~50 $ | ~50 $ |

Les enquêtes seules coûtent 50 à 78 $ selon le taux d'escalade. Fable estimait 45 à
65 $ avec les coûts unitaires de son propre plan ; l'écart vient surtout de la
rédaction dédiée (E9) et du cadrage, ajoutés dans ce plan. Le plafond de 18 brouillons
par jour borne la dépense : l'uniformité de profondeur entre N2 et N3 coûte peu.

## Sensibilité

| Scénario | Effet mensuel | Lecture |
|---|---|---|
| Référence (escalade 25 %) | ~77 $ | |
| **Fin du programme de tokens gratuits OpenAI** | +~25 $ (petits rôles sur `gpt-6-luna` ~15 $, second avis sur `gpt-6.1-terra` ~10 $) | Le système ne dépend pas du programme |
| Palier d'usage OpenAI élevé (10 M / 1 M par jour) | −~1 $ | Plus de repli payant |
| Démarrage à froid (pas de classifieur, pas de cache, pas de fiches) | +~10 à 15 $ le premier mois | Plus d'extractions et d'enquêtes |
| Atlas tranche 70 % des affirmations sans recherche web | −~8 $ | Objectif de la phase V3 |
| Recherche à 0,01 $ au lieu de 0,005 $ | +~12 $ | |
| Budget atteint : escalade suspendue | plafonne au niveau « 10 % » | Dégradation prévue |
| Volume × 3 (60 000 posts/jour) | +~10 $ (masse, repli payant) ; enquêtes inchangées | Le quota de brouillons plafonne les enquêtes |
| Vague majeure (100 000 posts sur un récit en 24 h) | +~1 à 3 $ | Une collecte par récit, variantes jugées sur le dossier, copies sans modèle |
| Liste suivie à 1 000 posts/jour | API X → ~150 $ | Premier poste du projet, devant toute l'IA |

## RAM sur Railway

Railway facture environ **10 $ par Go et par mois** de mémoire utilisée (et 20 $ par
vCPU actif et par mois). La RAM est le coût fixe à surveiller.

| Choix | RAM | Coût mensuel indicatif |
|---|---|---|
| Modèle d'embeddings local type bge-m3 | ~2 à 2,5 Go par réplica | ~20 à 25 $ par réplica, en permanence |
| Petit modèle local multilingual-e5-small (ONNX int8) | ~0,2 à 0,3 Go | ~2 à 3 $, qualité inférieure |
| **API d'embeddings (retenu)** | 0 | ~0,80 $ de tokens |
| Index HNSW sur tous les vecteurs de posts (écarté, Fable Q7) | ~5 Go par an d'index | ~50 $/mois au bout d'un an |
| **Index HNSW sur les groupes seulement (retenu)** | ~170 Mo par an | ~2 $/mois au bout d'un an |

Dimensionnement prévu :

| Réplica | Services | RSS cible | Coût |
|---|---|---|---|
| Départ : **un** réplica hangar dédié | `factcheck,investigate,atlas` | ≤ 250 Mo | ~2,5 $ / mois |
| Plus tard, si la charge le justifie | réplicas séparés par file (`TOWER_QUEUES`) | ~120 à 200 Mo chacun | ~1,2 à 2 $ chacun |
| PostgreSQL | index vectoriel des groupes seulement, vecteurs de posts sans index (14 jours, partitions hebdomadaires) | +~0,2 à 0,3 Go la première année | +~2 à 3 $ |

On commence avec un seul réplica et on sépare par configuration quand une file prend
du retard (`TOWER_SERVICES`, `TOWER_QUEUES`), sans modifier le code. Chaque processus
Python coûte environ 70 Mo de base : multiplier les réplicas trop tôt gaspille de la
RAM.

Mesures à faire au premier smoke test : RSS au repos et en charge de chaque réplica,
coût d'import des SDK `openai` et `anthropic` (importés paresseusement) et de la
bibliothèque d'extraction HTML.

## Leviers d'économie, par ordre

1. Ne pas appeler de modèle : empreintes, copies, classifieur, bandes de valeurs
   validées.
2. Tokens gratuits OpenAI pour tous les petits rôles et le second avis.
3. Lots de 10 avec préfixe court (900 tokens) et cache de prompt.
4. Cache de contenu des appels (`llm_calls`) : jamais deux fois le même appel.
5. Atlas avant la recherche web.
6. Profondeur renforcée seulement quand l'affirmation est difficile.
7. API Batch (−50 %) pour tout ce qui tolère la latence (juge N2, audit, lots N1
   payants).
8. Quota de brouillons : la dépense d'enquête suit la capacité de relecture.
9. Dégradation quand un budget est atteint ([niveaux.md](niveaux.md)).
