# Récits et vagues de rumeurs

Une rumeur qui explose ne ressemble pas à un post isolé : ce sont des milliers de
posts, sur plusieurs plateformes, qui reformulent la même chose avec des variantes
(chiffres arrondis, image recadrée, traduction, ajout d'une anecdote). Traiter chaque
post serait ruineux et produirait des verdicts incohérents. Le système doit
**reconnaître la vague tôt, enquêter une fois, appliquer partout, tenir la charge**.

## Trois niveaux de regroupement

| Niveau | Contenu | Exemple | Partage |
|---|---|---|---|
| Copie | Posts au texte quasi identique (SimHash) | Le même message copié par 300 comptes | Mêmes affirmations, sans appel de modèle |
| Groupe d'affirmations | Affirmations à signature compatible | « 12 000 migrants arrivés hier à Calais » et « plus de 10 000 migrants hier à Calais » | Même verdict (si les valeurs sont compatibles) |
| **Récit** | Groupes qui racontent la même histoire, même avec des affirmations différentes | « Les migrants de Calais ont reçu des logements sociaux » + « La mairie de Calais a expulsé des familles pour loger des migrants » + photo d'un camp présentée comme récente | Même enquête de fond, même note de contexte de base, même alerte |

Le **récit** est nouveau par rapport au plan de Fable. Il regroupe des groupes
d'affirmations :

- par proximité d'embedding du centroïde des groupes, dans une fenêtre de temps ;
- par co-occurrence (les mêmes posts ou les mêmes comptes portent plusieurs groupes) ;
- par entités partagées (lieu, personne, événement) ;
- par médias partagés (même image ou même vidéo, voir
  [plateformes-medias.md](plateformes-medias.md)).

Le rattachement est **incrémental** (pas de regroupement global recalculé chaque nuit,
écarté par Fable pour l'instabilité des groupes). Les fusions et scissions de récits
sont journalisées et réversibles, comme celles des groupes. Un récit a un titre court
neutre rédigé par un petit modèle et validé par un relecteur.

## Détection d'une vague

L'ordonnanceur E4 calcule toutes les 2 minutes, par récit, des agrégats SQL sur
fenêtres glissantes (5 min, 1 h, 6 h) :

| Signal | Mesure |
|---|---|
| Vélocité | nouveaux posts par fenêtre, rapportés à la moyenne du récit et à la moyenne du thème |
| Accélération | rapport entre la fenêtre courante et la précédente |
| Diversité | nombre de comptes distincts, d'installations distinctes, de plateformes |
| Portée | vues et engagement cumulés (estimés, confirmés par une lecture API sur un post représentatif) |
| Mutation | nouveaux groupes rattachés au récit dans la dernière heure (la rumeur se déforme) |

Le score de vague combine ces signaux par un **z-score robuste** (médiane et écart
absolu médian sur l'historique du thème) : on détecte un pic par rapport à la normale,
pas un volume absolu. Trois paliers :

| Palier | Condition indicative | Effet |
|---|---|---|
| Veille | z ≥ 3 sur la vélocité 1 h | Récit marqué, facteur de priorité × 2 |
| Vague | z ≥ 5, ou ≥ 3 plateformes, ou reprise par un compte N3 | Mode vague (ci-dessous), niveau N2 minimum |
| Alerte | z ≥ 8, ou thème sous surveillance (santé, élections, sécurité, catastrophe) | Niveau N4, alerte Discord immédiate |

Les seuils se calibrent sur l'historique réel une fois l'ingestion en place.

## Mode vague

Quand un récit entre en vague :

1. **Une enquête, pas mille.** L'ordonnanceur choisit le groupe représentatif du récit
   (le plus repris, le plus précis) et lance **une** enquête au niveau du récit. Les
   autres groupes du récit sont jugés ensuite par comparaison de signature et fiche de
   référence (coût de E5), ou par une mini-enquête qui réutilise les passages déjà
   collectés.
2. **Voie rapide.** File dédiée `fc-surge` (ou priorité maximale dans `fc-deep`),
   quota de brouillons **réservé** et séparé du quota quotidien (par exemple 3 par
   jour), budget puisé dans la réserve de N4.
3. **Note de contexte d'abord.** En pleine vague, les faits sont souvent incomplets.
   Le premier brouillon peut être une **note de contexte** prudente (« L'image date de
   2019, elle a été prise à… », « Aucune source officielle ne confirme à cette
   heure… ») avant un verdict complet. La note précise l'heure de vérification.
4. **Application à toutes les variantes.** Une fois validé, le verdict ou la note
   s'applique à tous les groupes du récit dont la signature est compatible ; les
   nouvelles variantes qui arrivent sont rattachées et héritent du verdict sans
   nouvelle enquête (surveillance passive). Les variantes incompatibles (valeur
   différente, autre lieu) sont signalées au relecteur au lieu d'être couvertes
   silencieusement.
5. **Revalidation rapprochée.** Pendant une vague, la péremption est courte (situation
   en cours : 6 à 24 h) : un job relit les sources toutes les quelques heures et
   signale tout changement.
6. **Fin de vague.** Quand le score retombe sous le palier « veille » pendant 6 h :
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

Ces signaux apparaissent dans le brouillon comme **informations de contexte pour le
relecteur**. Ils ne modifient jamais le verdict : un contenu vrai partagé de façon
coordonnée reste vrai. Ils ne dépendent pas du bord politique supposé des comptes.

## Tenir la charge pendant une vague

Une vague peut multiplier le flux par 10 ou 100 en quelques heures. Le système ne doit
ni exploser la RAM, ni exploser le budget.

| Risque | Parade |
|---|---|
| Flot d'insertions | `observations` : au-delà d'un seuil par contenu, on agrège en **compteurs** (par contenu, par heure, par source) au lieu d'une ligne par observation. Les copies exactes (même empreinte) n'insèrent qu'un compteur. |
| Flot d'extractions | Les copies (SimHash) et quasi-doublons (E1) héritent des affirmations sans appel de modèle. En vague, la part de copies est justement très élevée. |
| Coût des modèles | Une seule enquête par récit ; budget de vague plafonné ; au-delà, les variantes attendent la fin de l'enquête principale. |
| Files saturées | Contre-pression : l'ordonnanceur n'enfile pas plus que la capacité mesurée de chaque file ; les jobs N1 sont différés (lot suivant) avant les N3/N4. |
| Échantillonnage | Au-delà d'un débit par récit (par exemple 200 posts par minute), seul un échantillon aléatoire stratifié (par plateforme et par heure) passe en E2 ; le reste est compté. Les estimations de portée restent justes, le coût reste borné. |
| RAM | Aucun état en mémoire : tout est en base, chaque réplica traite des lots courts. Plus de réplicas hangar si besoin, sans changer le code. |
| Lectures API payantes | Une lecture par récit représentatif et par fenêtre, jamais par post. |

## Données

Tables `narratives`, `narrative_groups` (lien groupe ↔ récit, avec historique) et
`narrative_stats` (agrégats par fenêtre), décrites dans [donnees.md](donnees.md).
