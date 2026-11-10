# Récits et vagues de rumeurs

Une rumeur qui explose ne ressemble pas à un post isolé : ce sont des milliers de
posts, sur plusieurs plateformes, qui reformulent la même chose avec des variantes
(chiffres arrondis, image recadrée, traduction, ajout d'une anecdote). Traiter chaque
post serait ruineux et produirait des verdicts incohérents. Le système doit
**reconnaître la vague tôt, collecter les preuves une fois, juger chaque variante à bas
coût, tenir la charge**.

Règles revues après l'avis de Fable ([reponses-fable.md](reponses-fable.md), Q4 à Q6).

## Un récit partage des preuves, pas un verdict

Le récit a deux fonctions qu'on sépare :

| Fonction | Exigence de regroupement | Assurée par |
|---|---|---|
| **Compter et détecter** (vélocité, alerte, vue d'ensemble pour l'équipe) | Lâche : mieux vaut un récit un peu large qu'une vague fragmentée | Rattachement des groupes aux récits (ci-dessous) |
| **Appliquer un verdict** | Strict : une variante différente ne doit jamais hériter d'un verdict qui ne la concerne pas | La signature et les bandes de valeurs, au niveau du **groupe** ([pipeline.md](pipeline.md), E3) |

Le récit porte un **dossier de preuves** (passages collectés, fiches de référence,
notes de contexte rattachées à des objets) que toutes ses variantes réutilisent. Chaque
groupe du récit reçoit **son propre verdict**, calculé à bas coût sur ce dossier.

## Trois niveaux de regroupement

| Niveau | Contenu | Exemple | Partage |
|---|---|---|---|
| Copie | Posts au texte quasi identique (SimHash, distance ≤ 3) | Le même message copié par 300 comptes | Mêmes affirmations, sans appel de modèle |
| Groupe d'affirmations | Affirmations qui se résolvent vers la même fiche et la même bande de valeurs | « 12 000 migrants arrivés hier à Calais » et « 12 000 migrants hier à Calais » | Même verdict |
| **Récit** | Groupes qui racontent la même histoire | « Les migrants de Calais ont reçu des logements sociaux » + « La mairie a expulsé des familles pour les loger » + photo d'un camp présentée comme récente | Même dossier de preuves, même alerte, verdicts distincts |

## Rattachement d'un groupe à un récit

Incrémental (pas de regroupement global recalculé chaque nuit). Un groupe rejoint un
récit si les **trois** conditions sont réunies :

1. **Au moins deux signaux sur quatre**, dont au moins un non sémantique :
   - sémantique : proximité d'un des médoïdes du récit (ci-dessous) ;
   - co-occurrence : les deux affirmations apparaissent ensemble dans au moins 3 posts
     de plus de 3 comptes distincts ;
   - entité partagée ;
   - média partagé.
2. **Au moins une ancre spécifique partagée** :
   - une entité rare, pondérée par sa rareté sur 30 jours (« Calais » est une ancre,
     « immigration » n'en est pas une) ;
   - un média non générique ;
   - un lien.
   Un média vu dans de nombreux récits sans rapport (mème, logo) est exclu des ancres.
3. **Proximité d'un médoïde** : tout membre doit rester proche d'au moins un médoïde.

### Médoïdes plutôt que centroïde

Un centroïde dérive à mesure que des groupes s'ajoutent. Le récit est représenté par
**1 à 3 médoïdes**, qui sont des affirmations réelles :
- le groupe fondateur, fixe ;
- les deux groupes à plus forte portée, réélus au plus une fois par jour.

### Bornes

- **Clôture** après 7 jours sans activité. Une résurgence ultérieure crée un nouveau
  récit, lié à l'ancien et qui hérite de son dossier de preuves.
- **Scission proposée** au relecteur au-delà de 25 groupes, ou quand deux
  sous-ensembles ne tiennent ensemble que par un seul groupe (pont).
- Fusions et scissions journalisées et réversibles. Le risque restant (une ancre
  partagée par hasard, comme un nom de ville dans deux affaires) est couvert par
  cette réversibilité.

Un récit a un titre court et neutre, rédigé par un petit modèle et validé par un
relecteur.

## Détection d'une vague

### Score

L'ordonnanceur E4 calcule toutes les 2 minutes, par récit, sur des fenêtres de 5 min,
1 h et 6 h :

- la **part du flux observé** : posts du récit ÷ posts observés sur la même fenêtre.
  Cette normalisation neutralise la croissance de la base d'utilisateurs, le cycle
  jour-nuit et l'arrivée de nouvelles plateformes, sans jeton d'installation ;
- la diversité (comptes distincts) ;
- l'accélération (rapport à la fenêtre précédente).

L'écart à l'attendu suit une forme de type Poisson avec plancher :
`(observé − attendu) ÷ √(attendu + 5)`. Ce choix remplace le z-score robuste, qui
dégénère sur des comptages creux : la plupart des fenêtres de 5 min valent zéro, donc
leur médiane et leur écart absolu médian aussi. Des **planchers absolus** restent une
condition nécessaire en permanence.

### Paliers

| Palier | Condition de départ | Effet |
|---|---|---|
| **Veille** | ≥ 1 % du flux de l'heure et ≥ 10 comptes distincts | Récit marqué, facteur de priorité × 2 |
| **Vague** | ≥ 3 % du flux, ≥ 30 comptes et doublement par rapport à l'heure précédente ; **ou** palier veille franchi séparément sur 2 plateformes ; **ou** reprise par un compte N3 | Mode vague (ci-dessous), niveau N2 minimum |
| **Alerte** | ≥ 8 % du flux et ≥ 80 comptes, ou thème sous surveillance (santé, élections, sécurité, catastrophe) | Niveau N4, alerte Discord immédiate |

### Calibration progressive

| Phase | Règle |
|---|---|
| Jusqu'à 1 000 récits clos | Seuils absolus ci-dessus |
| Au-delà | Quantiles globaux : « top 1 % des récits au même âge » |
| À partir de 300 récits clos dans un thème | Quantiles par thème, rétrécis vers le global |

Piège : tout changement des filtres serveur de l'extension (mots-clés, comptes,
seuils) ressemble à une vague. Les filtres sont **versionnés**, et les bases de
comparaison sont gelées pendant 24 h après chaque changement.

Risque restant : deux vagues simultanées gonflent le dénominateur et s'atténuent
l'une l'autre. Les planchers absolus limitent l'effet.

## Mode vague

Quand un récit entre en vague :

1. **Collecte unique.** L'ordonnanceur choisit le groupe représentatif (le plus repris,
   le plus précis) et lance une enquête complète. Ses passages, fiches et notes
   alimentent le **dossier de preuves** du récit.
2. **Variantes jugées à bas coût.** Les autres groupes du récit sont jugés sur ce
   dossier, à la façon de la vérification légère E5 (petit modèle, sans nouvelle
   collecte sauf manque explicite). Seules les variantes **à forte portée** montent
   au relecteur : signaler chaque variante incompatible saturerait les quelque 12
   relectures quotidiennes.
3. **Voie rapide.** File `fc-surge` (ou priorité maximale dans `fc-deep`), quota de
   brouillons **réservé** et séparé du quota quotidien (3 par jour au départ), budget
   puisé dans la réserve de N4.
4. **Note provisoire d'abord.** En pleine vague, les faits sont souvent incomplets. Le
   premier brouillon peut être une note de type `developing` (« Aucune source
   officielle ne confirme à cette heure… »). Elle est **horodatée**, expire en 6 h au
   plus sans revalidation, et n'est **jamais héritée** après expiration (voir
   [contexte.md](contexte.md)).
5. **Héritage strict.** Un verdict validé ne s'applique qu'aux groupes de même
   signature normalisée ou de même bande de valeurs validée. Une note rattachée à un
   objet (par exemple la date réelle d'une image) s'applique partout où l'objet
   apparaît et où la présentation le contredit.
6. **Revalidation rapprochée.** Pendant une vague, la péremption est courte (situation
   en cours : 6 à 24 h) ; un job relit les sources toutes les quelques heures.
7. **Fin de vague.** Quand le récit repasse sous le palier « veille » pendant 6 h :
   `narrative.surge_ended`, retour aux niveaux normaux, rapport de vague (durée, posts,
   plateformes, coût, délai jusqu'au premier brouillon).

## Signaux de coordination

Une vague peut être organique ou orchestrée. Le système **signale** sans conclure (la
qualification relève de l'équipe) :

- nombreux comptes publiant un texte quasi identique (même SimHash) dans une fenêtre
  de quelques minutes ;
- comptes très récents ou à activité synchronisée ;
- installations de l'extension qui signalent en rafale le même récit (poids plafonné
  par installation, confirmation par l'API avant toute promotion) ;
- même média réutilisé avec des légendes contradictoires.

Ces signaux apparaissent dans le brouillon comme informations de contexte pour le
relecteur. Ils ne modifient jamais le verdict : un contenu vrai partagé de façon
coordonnée reste vrai. Ils ne dépendent pas du bord politique supposé des comptes.

## Tenir la charge pendant une vague

| Risque | Parade |
|---|---|
| Flot d'insertions | `observations` (table partitionnée) : au-delà d'un seuil par contenu, agrégation en **compteurs** par contenu, heure et source ; les copies exactes n'insèrent qu'un compteur |
| Flot d'extractions | Copies (SimHash) et quasi-doublons héritent des affirmations sans appel de modèle ; en vague, la part de copies est très élevée |
| Coût des modèles | Une collecte par récit ; variantes jugées sur le dossier ; budget de vague plafonné |
| Files saturées | Contre-pression : l'ordonnanceur n'enfile pas plus que la capacité mesurée de chaque file ; les jobs N1 sont différés avant les N3/N4 |
| Échantillonnage | Au-delà d'un débit par récit (200 posts par minute au départ), seul un échantillon aléatoire stratifié (plateforme, heure) passe en extraction ; le reste est compté |
| RAM | Aucun état en mémoire : tout est en base, lots courts, réplicas ajoutés par configuration |
| Lectures API payantes | Une lecture par récit représentatif et par fenêtre, jamais par post |

### Ne pas rater la variante qui relance la vague

L'échantillonnage aléatoire peut rater **la** nouveauté décisive (un nouveau chiffre,
une nouvelle accusation, une vidéo). Mais une voie « nouveauté » sans garde serait une
cible : un attaquant pourrait générer des chiffres aléatoires pour vider le budget (à
200 posts par minute, l'extraction épuiserait l'allocation du jour en 40 minutes).

Le déclencheur est donc **une nouveauté qui prend**, pas une nouveauté vue une fois :

1. **Empreinte de chaque nouveauté**, calculée par du code : chiffre normalisé, lien,
   mention, hachage de média, entité.
2. **Passage forcé** quand une empreinte atteint 3 posts de 3 comptes distincts, ou
   qu'un post dépasse le 90e centile d'engagement du récit.
3. **Un seul exemplaire** part en extraction : le post le plus engagé.
4. **Plafond** de 20 % du budget de tokens du récit, classement par
   soutien × engagement.

Le coût suit alors le nombre de variantes émergentes, quelques-unes par heure. La
détection d'entités sans modèle étant faible hors alphabet latin, elle est complétée
par la distance aux médoïdes.

## Données

Tables `narratives` (médoïdes, état, dates d'ouverture et de clôture, lien vers le récit
précédent en cas de résurgence), `narrative_groups` (lien groupe ↔ récit, avec
historique), `narrative_stats` (agrégats par fenêtre), `narrative_novelty`
(empreintes de nouveautés et leurs compteurs) et `extension_filter_versions`. Voir
[donnees.md](donnees.md).
