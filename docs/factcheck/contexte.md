# Issues, notes de contexte et cadrage

Un post n'est pas seulement vrai ou faux. Beaucoup de contenus trompeurs sont
**exacts mais incomplets** : chiffre réel sur une période choisie, photo authentique
d'un autre événement, citation tronquée, loi en projet présentée comme adoptée. Jules
demande un système de **contexte** en plus du fact-check. Ce plan sépare donc deux
axes indépendants.

## Deux axes

| Axe | Question | Valeurs |
|---|---|---|
| **Véracité** | L'affirmation est-elle exacte, selon les sources ? | Vrai, Plutôt vrai, Trompeur, Faux, Non vérifiable |
| **Contexte** | Un lecteur raisonnable serait-il induit en erreur sans information complémentaire ? | Aucun besoin, Contexte utile, Contexte nécessaire |

Exemples :

| Post | Véracité | Contexte |
|---|---|---|
| « Le chômage a baissé de 0,3 point au T3 » (chiffre INSEE exact) | Vrai | Aucun besoin |
| « Le chômage a explosé sous ce gouvernement » avec un graphique qui commence au point bas | Trompeur | Contexte nécessaire : période choisie |
| Photo authentique d'une émeute de 2019 présentée comme d'hier | Faux (sur la date) | Contexte nécessaire : média recyclé |
| « Le Parlement a voté la fin de X » alors que seule une commission l'a adopté | Trompeur | Contexte nécessaire : procédure en cours |
| Citation exacte d'un ministre, coupée avant la négation qui suivait | Trompeur | Contexte nécessaire : citation tronquée |
| Prédiction ou opinion | Non vérifiable | Éventuellement : faits utiles autour de l'opinion |

« Contexte manquant » (issue du plan de Fable) correspond à : véracité « Trompeur » ou
« Non vérifiable » avec contexte « nécessaire ». La rétrogradation automatique d'un
« Vrai » ou d'un « Faux » sans deux sources indépendantes devient : véracité
« Non vérifiable », contexte « utile » si des éléments partiels existent.

## Types de contexte

Liste fermée, utilisée par E2 (indices), E6 (cadrage), E8 (jugement) et E9 (rédaction).
Chaque type a une **consigne de rédaction** et des **sources attendues**.

Taxonomie à **15 types**, revue avec Fable ([reponses-fable.md](reponses-fable.md),
Q10). Chaque type a un **régime** de réutilisation (voir plus bas).

| Code | Type | Signal typique | Source attendue | Régime |
|---|---|---|---|---|
| `outdated` | Information ou chiffre périmé | Chiffre ancien présenté comme actuel ; détecté sans modèle quand la valeur correspond à la fiche précédente de l'indicateur | Dernière publication de la source | Objet (indicateur) |
| `recycled_media` | Média authentique recyclé | Média plus ancien que l'événement | Première apparition du média (voir [plateformes-medias.md](plateformes-medias.md)) | Objet (média) |
| `manipulated_media` | Média retouché, monté ou généré | Incohérences, version d'origine différente | Version d'origine, analyse de provenance (jamais un détecteur seul) | Objet (média) |
| `cherry_picking` | Période, échantillon ou point de comparaison choisis | « Depuis 2023 », « record » | Série longue, comparaisons | Formulation (groupe) |
| `missing_baseline` | Absolu contre relatif, rapport à la population | « 10 000 cas » sans dénominateur | Population, taux | Formulation (groupe) |
| `quote_integrity` | Citation tronquée, mal attribuée ou inventée | Citation courte entre guillemets | Transcription ou vidéo complète | Objet (citation) |
| `misrepresented_source` | La source invoquée ne dit pas cela | « Selon une étude… », « l'INSEE dit que… » | Le document cité lui-même | Objet (document cité) |
| `causation` | Corrélation présentée comme causalité | « À cause de », « depuis que » | Études, consensus | Formulation (groupe) |
| `definition` | Définition ambiguë ou changeante | Chômage, pauvreté, immigré ou étranger, délinquance | Définition officielle de la source | Objet (indicateur) |
| `scope` | Autre territoire ou autre population | Chiffre étranger appliqué à la France, chiffre d'une catégorie appliqué à tous | Source du territoire ou de la population | Formulation (groupe) |
| `procedure` | Procédure en cours présentée comme achevée | Projet de loi, enquête, mise en examen | Légifrance, dossier législatif, communiqué judiciaire | Objet (texte juridique avec version, événement) |
| `scientific_uncertainty` | Consensus ou incertitude scientifique | Étude isolée, prépublication | Revues, agences sanitaires | Formulation (groupe) |
| `satire` | Origine satirique reprise au premier degré | Compte parodique, site satirique | Contenu satirique d'origine | Objet (contenu d'origine) |
| `missing_event_context` | Fait exact sans les circonstances | Vidéo coupée, chronologie partielle | Couverture de référence | Objet (événement) |
| `developing` | Situation en cours, faits non établis | « Aucune source officielle ne confirme à cette heure » | Communiqués, couverture en direct | **Provisoire** |

### Trois régimes de réutilisation

| Régime | Règle | Exemple |
|---|---|---|
| **Objet** | La note est rattachée à un **objet** (média, indicateur, texte juridique avec version, événement, document cité, citation) et s'applique partout où l'objet apparaît **et** où la présentation le contredit | La date réelle d'une photo vaut pour tous les posts qui la portent avec une autre date |
| **Formulation** | La note est liée au **groupe** : elle répond à une façon de présenter les faits | Une période choisie dans un graphique |
| **Provisoire** | Note horodatée, **expire en 6 h au plus** sans revalidation, **jamais héritée** après expiration | « À cette heure, aucune source officielle ne confirme… » |

Conséquence technique : les notes sur une image supposent une empreinte perceptuelle
calculée **dès l'ingestion**, même si la lecture d'images (OCR, description) arrive
plus tard ([plateformes-medias.md](plateformes-medias.md)).

## Format d'une note de contexte

Inspiré des notes de la communauté de X (courtes, sourcées, jugées utiles par des
lecteurs de sensibilités différentes) :

- **280 caractères au plus** hors liens, dans la langue du post (avec traduction
  française pour le relecteur), phrases factuelles ;
- commence par l'information manquante, pas par un jugement (« Cette photo date de
  2019 et montre… », pas « Faux ! ») ;
- au moins un lien vers une source primaire ou secondaire récupérée ;
- aucun adjectif de jugement, aucune hypothèse sur l'intention de l'auteur, aucune
  mention de son bord ;
- précise l'heure de vérification pour une situation en cours ;
- une version longue optionnelle (fiche de fact-check) pour le site.

### Contrôle de neutralité rédactionnelle

Avant le relecteur, chaque note passe :

1. les contrôles déterministes d'E9 (citations, liens, longueur, lexique chargé) ;
2. un **test de pontage** par un modèle critique (rôle `critic`, autre fournisseur
   que le rédacteur) avec une grille fermée : la note serait-elle jugée
   utile et juste par un lecteur qui partage l'opinion du post et par un lecteur qui
   la rejette ? Contient-elle une opinion, une généralisation, un mot chargé, un fait
   non sourcé ? Le critique ne réécrit pas, il signale ;
3. un **test miroir** sur un échantillon : la même note, appliquée à une affirmation
   de structure identique favorable au camp opposé, serait-elle formulée pareil ? Les
   paires miroir du jeu de référence servent à mesurer cela hors production
   ([evaluation.md](evaluation.md)).

## E6 — Le cadrage : prendre du recul

Le cadrage est l'étage qui fait « prendre du recul » au système. Il intervient avant
toute recherche, à partir de l'affirmation canonique et des indices d'E2 (jamais du
post brut ni de l'auteur).

### Sortie structurée

```json
{
  "literal_claim": "Ce que l'affirmation dit littéralement",
  "implied_claims": ["Sous-entendus raisonnables"],
  "charitable_reading": "L'interprétation la plus forte et la plus raisonnable",
  "key_definitions": [{"term": "chômage", "candidates": ["BIT (INSEE)", "catégorie A France Travail"], "note": "…"}],
  "time_frame": {"stated": "hier", "resolved": "2026-11-09", "confidence": "high"},
  "jurisdiction": "FR",
  "true_if": ["Conditions qui rendraient l'affirmation vraie"],
  "false_if": ["Conditions qui la rendraient fausse"],
  "known_pitfalls": ["cherry_picking", "definition"],
  "sub_questions": [{"question": "…", "expected_source_type": "statistic", "jurisdiction": "FR"}],
  "verifiability": {"verdict": "verifiable", "reason": "…"},
  "context_hypotheses": ["missing_baseline"]
}
```

### Ce que le cadrage apporte

- **Évite de vérifier la mauvaise chose** : un modèle qui cherche « le chômage est à
  9 % » sans savoir de quelle définition il s'agit trouvera une source qui confirme et
  une autre qui infirme.
- **Principe de charité** : on vérifie l'interprétation la plus forte, pas un homme
  de paille. Cela réduit les « Faux » injustes, première source de contestation.
- **Requêtes symétriques** : les conditions `true_if` et `false_if` alimentent des
  requêtes dans les deux sens (parade au biais de collecte asymétrique, voir
  [biais.md](biais.md)).
- **Décision précoce** : une opinion ou une prédiction sort en « Non vérifiable » sans
  enquête, ce qui économise le coût le plus élevé.
- **Temps** : la date du jour et la date du post sont fournies ; « hier » est résolu ;
  le modèle est averti que ses connaissances internes s'arrêtent à une date et ne
  doivent pas servir de preuve.

### Contexte du fil de discussion

Un post est souvent une réponse ou une citation. E2 reçoit le texte du post parent ou
cité (tronqué) et produit un champ `thread_context` court et neutre (« répond à un
post affirmant que… »). Le cadrage le reçoit sous cette forme reformulée, ce qui lui
permet de comprendre l'ironie, la réfutation ou la citation sans lire le texte brut.

## Le brouillon dans Discord

| Bloc | Contenu |
|---|---|
| En-tête | Issue de véracité, besoin de contexte, confiance calibrée, niveau, récit (si vague) |
| Affirmation | Affirmation canonique, lien vers le post représentatif, nombre de posts et de plateformes du groupe |
| Note de contexte | Texte proposé (≤ 280 caractères) |
| Justification | Assertions avec citations exactes et liens |
| Ce que les sources ne disent pas | Liste |
| Points à vérifier en priorité | Liste du juge + objection typée du second avis en cas de désaccord de fond + désaccord entre les deux passes du juge |
| Contexte du compte | (N3/N4) récidive, verdicts antérieurs, corrections publiées |
| Signaux de coordination | (si vague) informations pour le relecteur |
| Actions | Valider, corriger, rejeter, scinder le groupe, changer l'issue, forcer un niveau |

Le relecteur reste maître : il peut changer l'issue, la note, les sources. Chaque
changement est enregistré comme donnée d'apprentissage.
