# Niveaux de traitement

La puissance de calcul dépensée sur un contenu dépend de son **niveau**. Un tweet d'un
compte anonyme vu 40 fois ne mérite pas le traitement d'une déclaration de ministre ou
d'une rumeur vue un million de fois. Le niveau est la **clé** des réglages de
surveillance, de latence et de budget.

Le niveau s'applique au **groupe d'affirmations** (et au récit qui le contient, voir
[vagues.md](vagues.md)), pas au post : tous les posts d'un groupe héritent du niveau le
plus élevé atteint.

## Principe arbitré avec Fable

> Le rôle public et la portée achètent la **surveillance** et la **latence** ; la
> **difficulté** de l'affirmation achète la **profondeur**.

Toute enquête démarre à la même profondeur, quel que soit le niveau (N2 ou N3), et
passe en profondeur renforcée selon la difficulté constatée, jamais selon l'auteur.
Source : [reponses-fable.md](reponses-fable.md), Q1. Le plafond de 18 brouillons par
jour rend cette uniformité presque gratuite.

En N1, rien n'est publié : l'écart entre N1 et N3 est un écart de **sélection** (ce
qu'on choisit de vérifier), pas de traitement d'un verdict. Il est mesuré (voir plus
bas).

## Les cinq niveaux

| Niveau | Nom | Qui y entre | Ce qui tourne | Latence cible |
|---|---|---|---|---|
| **N0** | Écarté | Post non vérifiable (opinion, blague, trop court, langue non traitée), copie exacte d'un post déjà traité | Code seul (E0, E1) | — |
| **N1** | Masse | Tout post vérifiable non promu (extension, flux généraux) | Extraction en lot, appariement, vérification légère si le groupe est dans le haut du lot | 24 h (lot) |
| **N2** | Viral | Groupe promu par sa portée ou par un signal « probablement faux » | Enquête standard, juge en API Batch ; escalade selon la difficulté | 2 h |
| **N3** | Ciblé | Auteur dans la liste des comptes suivis (rôle public), ou groupe repris par un tel compte | Lecture par l'API de la plateforme, extraction en temps réel (1 post par appel), enquête standard immédiate ; escalade selon la difficulté | Triage 1 min, verdict 1 h |
| **N4** | Critique | Forçage manuel, thème sous surveillance avec vélocité au palier « alerte », **mode vague** au palier alerte | Profondeur renforcée d'office, réserve budgétaire, revalidation continue | 15 min |

Correspondance avec les profils de Fable : N1 = Masse, N2 = Viral, N3 = Ciblé,
N4 = Critique. N0 est explicite pour rendre visible ce qui est écarté sans aucun coût.

## Deux profondeurs d'enquête

| | Enquête standard (N2, N3) | Profondeur renforcée |
|---|---|---|
| Déclencheur | Toute enquête | Désaccord de fond avec le second avis, sources contradictoires, confiance calculée < 0,6 ; toujours en N4 |
| Cadrage E6 | Sonnet 5.5 | Opus 5.5 |
| Recherches | 4 (après l'Atlas) | 4 de plus (8), 2 sources primaires visées |
| Juge E8 | Sonnet 5.5 (API Batch en N2, synchrone en N3) | Opus 5.5 |
| Second avis | Grand modèle OpenAI, aveugle | Idem + objection typée |
| Seconde passe du juge | Non | Oui |
| Budget maximal unitaire | 0,10 $ | 0,40 $ (0,60 $ en N4) |

## Réglages par niveau

Valeurs par défaut, versionnées en base (`processing_profiles`, voir
[donnees.md](donnees.md)) et modifiables à chaud depuis Discord.

| Réglage | N1 Masse | N2 Viral | N3 Ciblé | N4 Critique |
|---|---|---|---|---|
| Seuil du classifieur E1 (écarté en dessous) | 0,15 | hérité de N1 | 0,05 | 0 (tout passe) |
| Extraction E2 | lot de 10 posts ou 2 500 tokens (plafond 12), file `fc-bulk` | hérité | 1 post par appel, temps réel | 1 post par appel |
| Vérification légère E5 | top 20 % des groupes nouveaux du lot | automatique | automatique | automatique |
| Enquête | — | standard, escalade si difficile | standard, escalade si difficile | renforcée d'office |
| Réutilisation d'un verdict | signature normalisée identique, ou bande de valeurs validée ([pipeline.md](pipeline.md), E3) | idem | idem | jamais directe : sources rechargées et rejugées |
| Quand le budget est atteint | file au lot suivant, puis E1 seul | groupe remis en file | escalade suspendue (enquête standard seulement) | réserve de 15 €, alerte Discord |

## Promotion (montée de niveau)

| Vers | Condition (une suffit) | Confirmation avant dépense |
|---|---|---|
| N1 | Post vérifiable (E1 au-dessus du seuil) | — |
| N2 | ≥ 5 observateurs indépendants en 6 h ; engagement × 3 en 1 h ; > 50 000 vues ; signal « probablement faux » avec portée au-dessus de la médiane ; groupe appartenant à un récit en vague | Une lecture API de la plateforme sur le post représentatif confirme les métriques |
| N3 | Un compte de la liste suivie publie ou reprend une affirmation du groupe | Aucune (la liste est déjà vérifiée) |
| N4 | Forçage manuel ; thème sous surveillance au palier alerte ; récit en vague au palier alerte | — |

La promotion est **monotone pendant la fenêtre active** : un groupe ne redescend pas
pendant une enquête en cours.

## Rétrogradation (descente de niveau)

- Portée retombée sous le seuil d'entrée pendant 24 h (N2 → N1).
- Verdict publié et stable : le groupe passe en **surveillance passive** (application
  du verdict aux nouvelles variantes compatibles, sans nouvelle enquête) jusqu'à
  péremption.
- Forçage expiré (chaque forçage a une durée).

Une rétrogradation n'efface rien : le verdict et les traces restent.

## Liste des comptes suivis et neutralité

La liste est **tenue par l'équipe**, qui choisit librement qui y figure (réponse de
Jules, 2026-11-11) : c'est un choix éditorial, pas un calcul du système. Sa
**publication** reste recommandée, car la composition de la liste est le premier biais
de sélection possible. Le système encadre ce que la liste peut changer :

- **autorisé** : le **rôle public**, appliqué de façon identique à tous et **dans tous
  les pays** (chef d'État ou de gouvernement, membre du gouvernement, parlementaire,
  élu local d'une grande ville, candidat déclaré, parti, média, institution,
  organisation internationale, compte au-delà d'un seuil d'abonnés à définir). Ce
  rôle ne change que la surveillance et la latence ;
- **interdit** : son bord politique, son historique de verdicts, sa popularité auprès
  de l'équipe. Aucun de ces éléments n'entre dans le niveau, la priorité, la
  profondeur ou le verdict ;
- le juge ne voit **jamais** l'auteur ni le niveau.

**Pays à pluralisme faible** : la liste réunit les titulaires de fonctions (registres
officiels, Wikidata) et les **50 comptes à plus forte audience du pays**, sans tri
éditorial. L'audience capte l'opposition, les exilés et les médias indépendants. En
couverture C ([international.md](international.md)), seules des notes de contexte
sont produites.

**Options écartées** (avec Fable) : des quotas de traitement par bord (le pipeline
devrait connaître le bord, et la neutralité est une égalité de traitement, pas de
résultats) ; une liste au prorata des sièges (un choix politique de plus, qui ignore
les acteurs hors parlement).

**Méthodologie publique** : le pouvoir en place sera plus vérifié, quel qu'il soit,
puisqu'il produit plus d'affirmations à forte portée. C'est assumé et écrit dans la
méthodologie publiée.

### Mesure de la sélection

Hors pipeline (table séparée des étiquettes, lue par l'analytique seule) :

- par bord, à rôle et décile de portée égaux, rapport brouillons ÷ affirmations
  vérifiables observées. **Alerte si l'écart entre deux bords dépasse 1,25 sur un
  trimestre** ;
- chaque mois, **30 groupes non promus** tirés au hasard reçoivent une enquête
  complète, pour estimer ce que la sélection fait rater.

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
| facteur vague | 1, ou 2 à 5 si le récit est en veille ou en vague (voir [vagues.md](vagues.md)) | 1 à 5 |
| coût estimé | coût moyen observé du niveau dans le journal des coûts | — |

Aucun terme ne dépend de l'auteur ni de son bord. Le poids d'une installation de
l'extension est plafonné dans le calcul des observateurs (résistance aux signalements
coordonnés).

## Quota de brouillons

Le goulot reste le temps de relecture humaine. Réponse de Jules (2026-11-11) : la
capacité s'adaptera ; on n'envoie pas tout en relecture, au plus **une cinquantaine de
brouillons par jour**.

- **Plafond** : 50 brouillons par jour (`processing_profiles`, modifiable à chaud).
- **Sélection** quand il y a plus de candidats que de places, dans l'ordre :
  1. les cas où le système **doute** (désaccord de fond avec le second avis, sources
     contradictoires, confiance moyenne ou basse) sur un contenu à forte portée ;
  2. les verdicts « Faux » ou « Trompeur » à forte portée ;
  3. le reste par score de priorité.
- Le surplus attend ou expire. Les groupes de variantes **déjà couverts** par un
  verdict validé (signature identique ou bande de valeurs validée) n'occupent pas de
  place : ils héritent sans relecture.
- **Rien n'est publié sans relecture humaine** (hypothèse retenue, à confirmer par
  Jules) : ni sur X, ni sur le site, ni dans l'extension. C'est aussi ce qui place
  Seat 12A dans l'exception de l'AI Act (article 50) pour les textes relus.
- Le quota est un plafond, pas un objectif : c'est le **budget** qui borne réellement
  la dépense ([couts.md](couts.md)).
- Le mode vague a un quota séparé et réservé (voir [vagues.md](vagues.md)).
