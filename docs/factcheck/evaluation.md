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
