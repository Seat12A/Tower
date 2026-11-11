# Évaluation

On ne met en service ni un modèle, ni un seuil, ni un prompt sans mesure sur un jeu de
référence. Grâce au cache de contenu (`llm_calls`) et aux versions enregistrées, une
évaluation se rejoue à l'identique et ne repaie que ce qui a changé.

## Jeu de référence

Point de départ arrêté par Fable : **300 affirmations en français**, constituées en
phase 0, avant tout code de jugement. Composition :

| Catégorie | Nombre indicatif | But |
|---|---|---|
| Fact-checks déjà publiés par des rédactions reconnues (issue connue) | 120 | Exactitude du verdict |
| Affirmations vraies banales | 40 | Éviter le biais vers « Faux » |
| Non vérifiables (opinions, prédictions) | 25 | Taux d'abstention correct |
| Besoin de contexte (un cas par type de [contexte.md](contexte.md)) | 25 | Qualité des notes |
| Tentatives d'injection (dont en micro-lot) | 30 | Robustesse |
| Paires pièges (même phrase, valeur ou négation différente) | 50 paires | Pas de fausse réutilisation du cache |
| Paires miroir (même structure, favorables à des bords opposés) | 25 paires | Égalité de traitement |
| Négations de faits établis, de toutes idéologies | 20 | Pas de fausse symétrie : abstention ou réponse « équilibrée » = erreur ([objectivite.md](objectivite.md)) |
| Questions réellement débattues | 15 | Pas d'excès de certitude : verdict tranché = erreur |
| Perturbations lexicales (euphémisme / dysphémisme) | 25 paires | Robustesse au vocabulaire |

Le jeu de départ est en français, mais le périmètre est mondial
([international.md](international.md)) : chaque langue ajoutée à la liste des langues
traitées reçoit d'abord un sous-ensemble de référence (au moins 50 affirmations,
dont des fact-checks publiés par des signataires de l'IFCN du pays), et le jeu
contient des **paires interlingues** (même affirmation dans deux langues : même
groupe attendu) et des **paires à pays échangés** (même structure, pays permutés :
même issue attendue). Une langue n'est activée que si elle atteint les seuils de
qualité.

Ajouts de ce plan : un **corpus de vagues** (récits réels avec leurs variantes, pour
mesurer regroupement et application du verdict), et plus tard un **corpus médias**
(images recyclées avec leur date d'origine). Chaque revue humaine enrichit le jeu
(échantillon stratifié, pour ne pas sur-représenter un thème).

## Métriques par étage

| Étage | Métrique | Cible initiale |
|---|---|---|
| E0-E1 | Rappel des posts vérifiables (mesuré sur les 2 % tirés au hasard) | ≥ 90 % |
| E2 | Fidélité des affirmations (jugée par relecteur sur échantillon) ; taux de contamination en micro-lot | ≥ 95 % ; 0 |
| E3 | Pureté des groupes ; **taux de fausse réutilisation** du cache | ≥ 95 % ; < 1 % |
| Récits | Pureté et complétude des récits sur le corpus de vagues | à fixer après la première mesure |
| Vagues | Délai entre le début d'une vague et le premier brouillon | < 2 h (N2), < 30 min (N4) |
| E5 | Précision du signal « probablement faux » | à fixer |
| E6 | Qualité du cadrage (bonne définition, bonne période) jugée par relecteur | ≥ 90 % |
| E7 | Part des affirmations tranchées sans recherche web payante | ≥ 50 % (V1), ≥ 70 % (V3) |
| E8 | Exactitude du verdict ; taux d'abstention ; accord juge-second avis (de fond et de style, par fournisseur) ; taux d'escalade | à fixer sur la référence |
| E9 | Citations valides ; notes de contexte acceptées sans modification | 100 % (contrôle déterministe) ; à mesurer |
| E10 | Calibration (courbe de fiabilité mensuelle) ; part des confiances ≥ 0,80 effectivement acceptées | écart moyen < 0,1 |
| E2 (lots) | Taux d'omission mesuré par le rejeu solo de 2 % ; contamination des canaris | ~3 % ± 1,5 point, à faire baisser ; 0 |
| E3 (signature) | Erreurs d'extraction de signature sur 50 paires pièges | à fixer |
| E11 | Temps de relecture ; coût par brouillon accepté | à suivre |

**Métrique reine** : taux de **faux confiants** (« Vrai » ou « Faux » erroné avec
confiance ≥ 0,8). Cible proche de zéro, quitte à augmenter l'abstention.

## Neutralité : cinq mesures

1. **Permutation** : rejouer le jeu en attribuant chaque affirmation à des auteurs de
   bords opposés. Comme le juge ne voit pas l'auteur, tout écart révèle une fuite
   d'identité. Écart attendu : nul. Ce test vérifie aussi le test unitaire qui
   interdit certains champs dans les entrées des rôles sensibles.
2. **Paires miroir** : issues identiques, confiances proches.
3. **Perturbations lexicales** (ajout de ce plan, d'après PolBiX) : remplacer un terme
   neutre par un euphémisme ou un dysphémisme ne doit pas changer l'issue.
4. **Parité en production** : par bord (étiquettes de la table séparée, lues par
   l'analytique seule, limitées aux partis, organisations et affiliations officielles
   d'élus et de candidats, jamais déduites pour un compte ordinaire), à portée et thème égaux : taux de passage à chaque étage,
   distribution des issues, délais, taux d'acceptation par les relecteurs. Indicateur
   principal : rapport brouillons ÷ affirmations vérifiables observées, à rôle et
   décile de portée égaux ; alerte si l'écart entre deux bords dépasse **1,25 sur un
   trimestre**. Tableau de bord mensuel.
5. **Ce que la sélection rate** : chaque mois, 30 groupes non promus tirés au hasard
   reçoivent une enquête complète.

Limite à écrire noir sur blanc : la composition de la liste des comptes suivis et de
la table des thèmes à risque biaise la sélection avant tout verdict. Elle est publiée
et suivie.

## Choix des modèles par rôle

Pour chaque rôle, les modèles candidats ([modeles.md](modeles.md)) sont évalués sur
le sous-ensemble pertinent du jeu. On retient **le moins cher qui atteint le seuil**
du rôle (en comptant comme gratuit un modèle couvert par l'allocation OpenAI). Même
méthode pour l'effort de raisonnement : on commence bas et on ne monte que si la
mesure le justifie.

## Outillage

- `tower factcheck eval --set <jeu> --profile <version> [--role <rôle>]` : rejoue le
  jeu, compare au résultat attendu, écrit un rapport (Markdown) et le coût.
- Les appels déjà en cache ne sont pas repayés.
- Un changement de prompt, de route ou de profil passe par une évaluation avant
  activation ; la version précédente reste disponible (retour arrière immédiat).

## Audit hebdomadaire (Fable)

Un lot hebdomadaire `claude-fable-5-2` relit les désaccords (juge contre second avis,
juge contre relecteur, deux passes du juge), propose des réglages de seuils et de
prompts, et génère des cas pièges pour le jeu de référence. Ses propositions sont des
**suggestions** soumises à l'équipe, jamais appliquées automatiquement.

## Format du jeu et commande

Implémenté au lot 3 (voir [../fact-check.md](../fact-check.md#évaluation)).

```bash
uv run tower factcheck eval --set jeu.jsonl [--limit N] [--report rapport.md] [--level N3|N4] [--e3-only]
```

Le jeu est un fichier **JSONL** : un cas par ligne, encodé en UTF-8.

| Champ | Obligatoire | Contenu |
|---|---|---|
| `id` | oui | Identifiant stable du cas |
| `claim` | oui | Affirmation canonique, dans sa langue d'origine (≤ 300 caractères) |
| `lang` | oui | Langue de l'affirmation (BCP 47) |
| `claim_fr` | non (recommandé si `lang` ≠ `fr`) | Version française pivot ; à défaut, `claim` est reprise telle quelle |
| `jurisdiction` | oui | Code ISO 3166-1, `EU` ou `INT` |
| `date` | oui | Date de publication de l'affirmation (`AAAA-MM-JJ`) : sert à résoudre « hier », « l'an dernier » |
| `claim_type` | non | Type d'affirmation (valeurs de `ClaimType`) ; défaut `other` |
| `post_text` | non | Texte du post, pour le rédacteur ; défaut : `claim` |
| `expected_veracity` | oui | `true`, `mostly_true`, `misleading`, `false`, `unverifiable` |
| `expected_context_need` | oui | `none`, `useful`, `necessary` |
| `expected_context_types` | non | Liste des types de contexte attendus |
| `category` | non | `published_factcheck`, `true_banal`, `unverifiable`, `context`, `injection`, `trap_pair`, `interlingual_pair`, `mirror_pair`, `established_fact_denial`, `debated`, `lexical`, `other` |
| `pair_id` | non | Identifiant commun aux deux cas d'une paire (piège, miroir, perturbation lexicale, pays échangés) |
| `group` | non | Étiquette de groupe attendu : les cas de même étiquette doivent finir dans le même groupe, des étiquettes différentes dans des groupes distincts (pureté, fusions à tort, regroupements manqués) |
| `pair_expect` | non (exige `pair_id`) | `same` ou `different` pour la paire `pair_id` ; à défaut, `trap_pair` vaut `different`, `interlingual_pair` vaut `same` |
| `reference` | non | Fait de référence qui sert à calculer la bande du cas : `{"value": 7.4, "unit": "percent", "period_start": "2025-10-01", "period_end": "2025-12-31", "territory": "FR", "definition_variant": "ilo", "previous_value": 7.3}` (seul `value` est obligatoire) |
| `expected_band` | non (exige `reference`) | `{"band": "within_1_tau", "side": "above"}` ou `{"band": "contains", "side": "none"}` |
| `expected_outdated` | non (exige `reference`) | `true` ou `false` : l'affirmation reprend la valeur précédente du fait |
| `source_url` | non | Fact-check publié qui fixe l'issue attendue (traçabilité) |
| `notes` | non | Remarques libres |
| `example` | non | `true` pour un cas d'exemple, exclu des métriques publiées |

Chaque cas passe par E6 à E10 comme une enquête de production (même code, même
routeur, mêmes contrôles), sans écrire d'enquête en base. Métriques du rapport :

| Métrique | Définition |
|---|---|
| Exactitude | Issue identique à l'attendu ; aussi par côté (Vrai/Plutôt vrai, Trompeur/Faux, abstention) |
| **Faux confiants** | Issue « Vrai » ou « Faux » erronée avec une confiance ≥ 0,80, rapportée au nombre de cas (métrique reine) |
| Abstention | Part des issues « Non vérifiable » ; une abstention sur `established_fact_denial` est comptée comme erreur, un verdict tranché sur `debated` aussi |
| Besoin de contexte | Exactitude du besoin ; rappel des types de contexte attendus |
| Citations valides | Citations trouvées mot pour mot ÷ citations rendues par le juge |
| Escalade | Part des enquêtes passées en profondeur renforcée |
| Paires | Part des paires miroir ou de perturbation dont les deux issues sont identiques |
| Coût et latence | Coût moyen par cas (micro-dollars, cache compris : un cas rejoué coûte 0) et latence moyenne et p95 |

Les résultats sont aussi donnés par catégorie et par langue.

### Couche E3 (appariement, groupes, bandes)

Dès que le jeu contient des paires ou des groupes attendus (`pair_id` avec attendu,
`group`) ou des cas avec `reference`, l'outil rejoue la décision E3 de production : même
extraction E2 (même constructeur d'entrée, même filtre de fidélité, cache `llm_calls` : un
rejeu coûte 0), signature normalisée avec la `date` du cas, embeddings du texte original
(et du français pivot si la langue n'est pas le français), `plan_match` avec les seuils du
profil actif, arbitre de paires via le routeur quand le plan le demande. Seules les paires
listées dans le fichier sont comparées (deux à deux au sein d'un même `pair_id` ou d'une
même étiquette `group`), jamais les N². Aucune écriture en base hors caches (`llm_calls`,
budgets, `source_cache`). Les groupes prédits sont l'union transitive des décisions JOIN.
La bande (et `outdated`) est calculée avec le domaine et les tolérances des réglages ; si
l'extraction n'a pas d'unité, celle de `reference` est utilisée.

| Métrique E3 | Définition |
|---|---|
| **Taux de faux réemploi** (cible < 1 %, affiché en premier avec ✅/❌) | Paires attendues `different` décidées JOIN ÷ paires attendues `different` |
| Fusions à tort | JOIN entre cas d'étiquettes `group` différentes (nombre et taux) |
| Regroupements manqués | NEW entre cas de même étiquette (nombre et taux) |
| Pureté des groupes | Pour chaque groupe prédit, part de l'étiquette majoritaire ; moyenne pondérée (cas sans étiquette ignorés) |
| Paires interlingues | Part des paires `interlingual_pair` décidées JOIN (cible : toutes) |
| Arbitre et coût | Nombre d'appels de l'arbitre (et part des paires), coût E3 (extraction, embeddings, arbitre) |
| Exactitude des bandes | Bande et côté conformes à `expected_band` ; exactitude de `expected_outdated` |

Le rapport ajoute, parmi les métriques E6-E10, la **part des affirmations réglées sans
recherche web payante** (E7, cible ≥ 50 % en V1) : part des cas dont l'enquête n'a fait
aucune recherche payante (le décompte vient d'un enveloppeur de l'atlas, sans toucher au
pipeline ; « not available » si rien n'a été mesuré).

`--e3-only` saute E6-E10 (exécution peu coûteuse de la couche d'appariement ; rapport et
résumé limités à la section E3). Avec `TOWER_LLM_FAKE=1` le résultat est déterministe ; les
huit derniers cas d'exemple (paires piège valeur et négation, paire interlingue en/de avec
marqueur `[fr: …]` dans `post_text`, bande et valeur périmée) donnent 0 % de faux réemploi.
Le fournisseur factice ne renseigne pas la juridiction des affirmations non françaises : la
paire interlingue d'exemple est donc en/de et non fr/en. Les cas
d'exemple de `tests/fixtures/factcheck_eval_examples.jsonl` ne servent qu'à démontrer
l'outil : ce ne sont pas des cas du jeu de référence.
