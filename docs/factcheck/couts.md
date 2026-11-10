# Coûts : argent et RAM

Toutes les estimations reposent sur le scénario de dimensionnement du plan de Fable
(20 000 posts observés par jour, 15 % vérifiables, 3 relecteurs pour environ 12
brouillons relus par jour), à corriger avec les volumes réels. Tarifs relevés le
2026-11-10 ([recherche.md](recherche.md)). Les coûts réels seront mesurés par
`llm_calls` et `budget_counters` dès le premier jour.

## Hypothèses de tokens

| Appel | Entrée | Sortie | Remarque |
|---|---|---|---|
| Extraction E2, micro-lot de 20 | ~1 500 d'instructions (en cache) + 20 × 200 | 20 × 120 | ~320 tokens par post |
| Arbitre E3 | ~250 | ~20 | |
| Vérification légère E5 | ~2 500 | ~300 | |
| Cadrage E6 | ~1 500 | ~800 | Sonnet effort `low` en N2, `medium` en N3 |
| Juge E8 N2 | ~5 000 | ~1 200 | Sonnet, API Batch (−50 %) |
| Juge E8 N3/N4, par passe | ~12 000 | ~2 500 | Opus ; 2e passe si désaccord ou confiance < 0,8 en N3 (~30 % des cas), toujours en N4 |
| Contradicteur | ~6 000 | ~800 | Grand modèle OpenAI gratuit |
| Rédaction E9 | ~3 000 | ~600 | Sonnet |
| Embeddings | posts ~80, passages ~160 | — | Pré-filtre lexical : seuls les 50 meilleurs passages d'une enquête sont vectorisés |

## Coût unitaire par niveau

| Niveau | Postes payants | Coût estimé |
|---|---|---|
| N0 | aucun | 0 |
| N1 (par post) | embedding (~0,0000016 $), extraction gratuite, part de vérification légère | **~0,000005 $ par post** |
| N2 (par enquête) | cadrage 0,011 $ + juge en lot 0,011 $ + rédaction 0,012 $ + 4 recherches 0,020 $ + 1 lecture X 0,005 $ | **~0,06 $** |
| N3 (par enquête) | cadrage 0,019 $ + juge Opus 1,3 passe 0,13 $ + rédaction 0,012 $ + 8 recherches 0,04 $ + 1 lecture X 0,005 $ | **~0,20 $** |
| N4 (par enquête) | cadrage Opus + juge Opus 2 passes + Sonnet en second contradicteur + 10 recherches, 3 tours | **~0,40 $** |

Les recherches web sont le premier poste variable. Chaque affirmation tranchée par
l'Atlas sans recherche (fiches, fact-checks, sources structurées) les supprime.

## Projection mensuelle (régime établi)

| Poste | Volume mensuel | Coût |
|---|---|---|
| Petits modèles OpenAI (extraction, arbitrage, vérification légère, requêtes) | ~51 M de tokens | **0 $** (allocation gratuite) |
| Contradicteur OpenAI | ~4 M de tokens | **0 $** (allocation gratuite) |
| Embeddings | ~40 M de tokens | ~0,80 $ |
| Recherches web de E5 (N1) | ~1 200 requêtes | ~1 $ (après le crédit gratuit Brave de 5 $) |
| N2 Viral | 300 enquêtes | ~18 $ |
| N3 Ciblé | 180 enquêtes | ~36 $ |
| N4 Critique et vagues | 30 enquêtes | ~12 $ |
| Audit hebdomadaire Fable 5.2 (lot) | 4 lots | ~10 $ |
| **Total IA et recherche** | | **~78 $ / mois** |
| API X (comptes suivis, confirmations, revérifications) | ~10 000 lectures | ~50 $ (hors budget IA, comme chez Fable) |

Comparaison avec le plan de Fable (87 $) : le flux de masse passe de 20 $ à environ
2 $ grâce aux tokens gratuits et aux micro-lots ; une partie de l'économie est
réinvestie dans la qualité (cadrage, rédaction dédiée, seconde passe du juge,
contradicteur systématique en N3). Le total est voisin, la qualité attendue plus
élevée, et la marge de manœuvre plus grande (voir les leviers ci-dessous).

## Sensibilité

| Scénario | Effet mensuel | Lecture |
|---|---|---|
| Référence | ~78 $ | |
| **Fin du programme de tokens gratuits OpenAI** | +~19 $ (petits rôles sur `gpt-6-luna` ~11 $, contradicteur sur `gpt-6.1-terra` ~8 $) | Le système ne dépend pas du programme |
| Organisation OpenAI au palier bas (2,5 M / 250 k par jour) | ~+0 $ en régime établi ; ~+6 $ au démarrage à froid | Les micro-lots tiennent dans 2,5 M |
| Démarrage à froid (pas de classifieur, pas de cache) | +~10 à 15 $ le premier mois | Plus d'extractions et d'enquêtes |
| Atlas tranche 70 % des affirmations sans recherche web | −~8 $ | Objectif de la phase V3 |
| Recherche à 0,01 $ au lieu de 0,005 $ | +~12 $ | |
| Juge N3 sur Sonnet au lieu d'Opus | −~18 $ | Dégradation prévue quand le budget N3 est atteint |
| Pas de seconde passe du juge en N3 | −~7 $ | À décider sur la mesure du taux de désaccord |
| Volume × 3 (60 000 posts/jour) | +~10 $ (masse) ; enquêtes inchangées | Le quota de brouillons plafonne les enquêtes |
| Vague majeure (100 000 posts sur un récit en 24 h) | +~1 à 3 $ | Une enquête par récit, copies sans modèle |
| Liste suivie à 1 000 posts/jour | API X → ~150 $ | Premier poste du projet, devant toute l'IA |

## RAM sur Railway

Railway facture environ **10 $ par Go et par mois** de mémoire utilisée (et 20 $ par
vCPU actif et par mois). La RAM est donc le coût fixe à surveiller.

| Choix | RAM | Coût mensuel indicatif |
|---|---|---|
| Modèle d'embeddings local type bge-m3 | ~2 à 2,5 Go par réplica | ~20 à 25 $ par réplica, en permanence |
| Petit modèle local multilingual-e5-small (ONNX int8) | ~0,2 à 0,3 Go | ~2 à 3 $, qualité inférieure |
| **API d'embeddings (retenu)** | 0 | ~0,80 $ de tokens |

Dimensionnement prévu :

| Réplica | Services | RSS cible | Coût |
|---|---|---|---|
| Départ : **un** réplica hangar dédié | `factcheck,investigate,atlas` | ≤ 250 Mo | ~2,5 $ / mois |
| Plus tard, si la charge le justifie | réplicas séparés par file (`TOWER_QUEUES`) | ~120 à 200 Mo chacun | ~1,2 à 2 $ chacun |
| PostgreSQL | index vectoriels bornés (fenêtres, `halfvec`, 512 dimensions) | +~0,3 à 0,5 Go la première année | +~3 à 5 $ |

On commence avec un seul réplica et on sépare par configuration quand une file prend
du retard : aucune modification de code n'est nécessaire (`TOWER_SERVICES`,
`TOWER_QUEUES`). Chaque processus Python coûte environ 70 Mo de base : multiplier les
réplicas trop tôt gaspille de la RAM.

Mesures à faire au premier smoke test : RSS au repos et en charge de chaque réplica,
coût d'import des SDK `openai` et `anthropic` (importés paresseusement), et de la
bibliothèque d'extraction HTML.

## Leviers d'économie, par ordre

1. Ne pas appeler de modèle : empreintes, copies, quasi-doublons, classifieur.
2. Tokens gratuits OpenAI pour tous les petits rôles et le contradicteur.
3. Micro-lots et cache de prompt (préfixes stables).
4. Cache de contenu des appels (`llm_calls`) : jamais deux fois le même appel.
5. Atlas avant la recherche web.
6. API Batch (−50 %) pour tout ce qui tolère la latence (juge N2, audit, lots N1
   payants).
7. Effort de raisonnement minimal par rôle, mesuré sur le jeu de référence.
8. Quota de brouillons : la dépense d'enquête suit la capacité de relecture.
9. Dégradation par niveau quand un budget est atteint ([niveaux.md](niveaux.md)).
