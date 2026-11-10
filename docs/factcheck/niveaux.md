# Niveaux de traitement

La puissance de calcul dépensée sur un contenu dépend de son **niveau**. Un tweet d'un
compte anonyme vu 40 fois ne mérite pas le traitement d'une déclaration de ministre ou
d'une rumeur vue un million de fois. Le niveau est la **clé unique** de tous les
réglages : modèles, profondeur de recherche, latence, budget.

Le niveau s'applique au **groupe d'affirmations** (et au récit qui le contient, voir
[vagues.md](vagues.md)), pas au post : tous les posts d'un groupe héritent du niveau le
plus élevé atteint.

## Les cinq niveaux

| Niveau | Nom | Qui y entre | Ce qui tourne | Modèle le plus cher atteint | Latence cible |
|---|---|---|---|---|---|
| **N0** | Écarté | Post non vérifiable (opinion, blague, trop court, hors langue), doublon exact d'un post déjà traité | Code seul (E0, E1) | Aucun | — |
| **N1** | Masse | Tout post vérifiable non promu (extension, flux généraux) | Extraction en micro-lot, appariement, vérification légère si le groupe est dans le haut du lot | Petit modèle OpenAI (gratuit) | 24 h (lot) |
| **N2** | Viral | Groupe promu par sa portée ou par un signal « probablement faux » | Cadrage, collecte Atlas, juge Sonnet en lot, contradicteur OpenAI si confiance basse, note de contexte | Claude Sonnet 5.5 | 2 h |
| **N3** | Ciblé | Auteur dans la liste des comptes suivis (rôle public), ou groupe repris par un tel compte | Extraction temps réel, cadrage, collecte approfondie, juge Opus, contradicteur toujours | Claude Opus 5.5 | Triage 1 min, verdict 1 h |
| **N4** | Critique | Forçage manuel, thème sous surveillance avec vélocité top 1 %, **mode vague** | Tout N3 + réserve budgétaire, recherche la plus profonde, revalidation continue | Claude Opus 5.5 (Fable sur forçage) | 15 min |

Correspondance avec les profils de Fable : N1 = Masse, N2 = Viral, N3 = Ciblé,
N4 = Critique. N0 est explicite pour rendre visible ce qui est écarté sans aucun coût.

## Réglages par niveau

Valeurs par défaut, versionnées en base (`processing_profiles`, voir
[donnees.md](donnees.md)) et modifiables à chaud depuis Discord.

| Réglage | N1 Masse | N2 Viral | N3 Ciblé | N4 Critique |
|---|---|---|---|---|
| Seuil du classifieur E1 (écarté en dessous) | 0,15 | hérité de N1 | 0,05 | 0 (tout passe) |
| Extraction E2 | micro-lot de 20, file `fc-bulk`, API Batch possible | hérité | micro-lot de 1 à 5, temps réel | temps réel |
| Vérification légère E5 | top 20 % des groupes nouveaux du lot | automatique | automatique | automatique |
| Cadrage E6 | non | oui (Sonnet, effort `low`) | oui (Sonnet, effort `medium`) | oui (Opus) |
| Sources Atlas | fiches, fact-checks, structurées | + Wikipédia, actualité, 4 recherches web | + 8 recherches, 2 sources primaires exigées | + 10 recherches, 3 tours |
| Juge E8 | — | Sonnet 5.5, API Batch | Opus 5.5 | Opus 5.5 |
| Contradicteur | — | OpenAI grand modèle si confiance < 0,7 | toujours (OpenAI) | toujours (OpenAI) + Sonnet |
| Réutilisation du cache de verdicts | similarité ≥ 0,90 + signature | ≥ 0,92 + signature | ≥ 0,93 + signature + arbitre | jamais directe : sources rechargées et rejugées |
| Budget maximal unitaire | 0,0005 $ / post | 0,08 $ / affirmation | 0,25 $ / affirmation | 0,60 $ / affirmation |
| Quand le budget est atteint | file au lot suivant, puis E1 seul | retour en E5, groupe remis en file | juge Sonnet au lieu d'Opus | réserve de 15 €, alerte Discord |

Les seuils de similarité sont indicatifs : ils dépendent du modèle d'embeddings et se
calibrent sur le jeu de référence (voir [evaluation.md](evaluation.md)).

## Promotion (montée de niveau)

| Vers | Condition (une suffit) | Confirmation avant dépense |
|---|---|---|
| N1 | Post vérifiable (E1 au-dessus du seuil) | — |
| N2 | ≥ 5 observateurs indépendants en 6 h ; engagement × 3 en 1 h ; > 50 000 vues ; signal « probablement faux » avec portée au-dessus de la médiane ; groupe appartenant à un récit en vague | Une lecture API X (ou de la plateforme) du post représentatif confirme les métriques |
| N3 | Un compte de la liste suivie publie ou reprend une affirmation du groupe | Aucune (la liste est déjà vérifiée) |
| N4 | Forçage manuel ; thème sous surveillance et vélocité top 1 % ; récit en mode vague au-delà du seuil d'alerte | — |

La promotion est **monotone pendant la fenêtre active** : un groupe ne redescend pas
pendant une enquête en cours.

## Rétrogradation (descente de niveau)

Un niveau élevé coûte cher. Il redescend quand il n'est plus justifié :

- portée retombée sous le seuil d'entrée pendant 24 h (N2 → N1) ;
- verdict publié et stable : le groupe passe en **surveillance passive** (on applique
  le verdict aux nouvelles variantes sans réenquêter) jusqu'à péremption ;
- forçage expiré (chaque forçage a une durée).

Une rétrogradation n'efface rien : le verdict et les traces restent.

## Liste des comptes suivis et neutralité

Le niveau N3 utilise l'identité de l'auteur, ce que la règle 10 de `CLAUDE.md` encadre
strictement. La règle appliquée :

- **autorisé** : le **rôle public** de l'auteur, appliqué de façon identique à tous et
  **dans tous les pays** (chef d'État ou de gouvernement, membre du gouvernement,
  parlementaire, élu local d'une grande ville, candidat déclaré, parti, média,
  institution, organisation internationale, compte au-delà d'un seuil d'abonnés à
  définir) ;
- **interdit** : son bord politique, son historique de verdicts, sa popularité auprès
  de l'équipe. Aucun de ces éléments n'entre dans le calcul du niveau, du score de
  priorité ou du verdict ;
- la liste est **publiée** et suivie : composition par bord mesurée hors pipeline
  (table séparée lue par l'analytique seule), pour détecter un déséquilibre de
  sélection, premier biais possible du système (voir [biais.md](biais.md)) ;
- le juge ne voit **jamais** le niveau ni l'auteur. Le niveau change la **profondeur**
  de la vérification, jamais la façon de juger.

## Score de priorité (ordonnanceur E4)

À niveau égal, l'ordonnanceur dépense le budget dans l'ordre :

```
priorité = portée × risque_thématique × vérifiabilité × nouveauté × facteur_vague
           ÷ coût_estimé(niveau)
```

| Terme | Calcul | Bornes |
|---|---|---|
| portée | `log1p(vues estimées) + 2 × log1p(observateurs indépendants) + vélocité normalisée` | — |
| risque thématique | table réglable : santé, élections, sécurité, catastrophes en haut | 1 à 3 |
| vérifiabilité | probabilité estimée en E2 | 0 à 1 |
| nouveauté | 1 si groupe nouveau, 0,3 si revalidation | — |
| facteur vague | 1, ou 2 à 5 si le récit est en vague (voir [vagues.md](vagues.md)) | 1 à 5 |
| coût estimé | coût moyen observé du niveau dans le journal des coûts | — |

Aucun terme ne dépend de l'auteur ni de son bord. Le poids d'une installation de
l'extension est plafonné dans le calcul des observateurs (résistance aux signalements
coordonnés).

## Quota de brouillons

Le goulot reste le temps de relecture humaine. Le nombre de brouillons envoyés dans
Discord par jour est plafonné à **1,5 × la capacité de revue** (18 par jour au scénario
supposé). Le surplus attend ou expire. Le mode vague a un quota séparé et réservé (voir
[vagues.md](vagues.md)).
