# Réponses de Fable aux questions d'architecture (2026-11-10)

Réponse de Fable au brief [questions-fable.md](questions-fable.md), transmise par
Jules et archivée telle quelle. Les arbitrages qui en découlent sont reportés dans
[../decisions.md](../decisions.md) et dans les pages du plan concernées.

---

Jules, voici mes réponses. Je m'écarte de la réponse précédente sur Q1, Q3, Q4, Q5, Q9
et sur la satire (Q10) ; je la confirme avec des ajouts sur Q2, Q6, Q7, Q8 et Q11.
Tous les seuils sont des valeurs de départ à calibrer sur le jeu de référence.

## Hypothèses fausses ou fragiles

- Q1 : en N1 rien n'est publié, donc l'écart entre N1 et N3 est un écart de sélection.
  L'écart de profondeur se joue entre N2 et N3.
- Q2 : ton contrôle de fidélité est écrit dans le mauvais sens (détail plus bas), et
  l'allocation de 2,5 M est partagée avec E3, E5 et les requêtes.
- Q3 : 200 revues ne suffisent pas pour une régression complète. Avec 15 % de refus,
  cela fait 30 cas négatifs pour une douzaine de coefficients.
- Q4 : « une seule enquête sert tout le récit » est la simplification qui crée ton
  dilemme. Un récit partage des preuves, pas un verdict.
- Q5 : un z-score par médiane et écart absolu médian dégénère sur des comptages creux.
  La plupart des fenêtres de 5 min valent zéro, donc médiane et écart valent zéro.
- Q7 : `UNIQUE (platform, external_id)` est impossible sur une table partitionnée par
  mois sans la clé de partition.
- Q8 : de mémoire, sans l'avoir revérifié, le pivot de SemEval-2025 n° 7 était
  l'anglais. Le résultat ne valide donc pas un pivot français.
- Q10 : ton propre exemple (« aucune source officielle ne confirme à cette heure »)
  n'entre dans aucun des 12 types.
- Q11 : ni le SimHash ni le domaine ne mesurent l'indépendance. Deux journaux qui
  relaient le même communiqué passent pour deux sources.

## Q1. Neutralité du niveau Ciblé

Option (c), reformulée : le rôle public achète la surveillance et la latence, la
difficulté de l'affirmation achète la profondeur.

- Règle : toute enquête démarre à la même profondeur (Sonnet, 4 recherches, second
  avis). Elle passe en profondeur renforcée (Opus, 4 recherches de plus, seconde
  passe) sur désaccord de fond avec le second avis, sources contradictoires ou
  confiance calculée sous 0,6. N4 y passe toujours.
- Ce que garde N3 : lecture par l'API, triage en temps réel, lots de 1, délai d'une
  heure.
- Raison : le plafond de 18 brouillons par jour rend l'uniformité presque gratuite.
  Avec les coûts unitaires de mon plan, j'estime 45 à 65 $ par mois selon le taux
  d'escalade, contre environ 55 $ aujourd'hui.
- (a) rejeté : corriger par bord oblige le pipeline à connaître le bord. Et la
  neutralité est une égalité de traitement, pas de résultats : si un bord publie plus
  de faux à portée égale, un quota fabrique un faux équilibre.
- (b) rejeté : le prorata des sièges est un choix politique de plus, et il ignore les
  acteurs hors parlement.
- Mesure : hors pipeline, par bord, à rôle et décile de portée égaux, le rapport
  brouillons ÷ affirmations vérifiables observées. Alerte si l'écart entre deux bords
  dépasse 1,25 sur un trimestre. Chaque mois, 30 groupes non promus reçoivent une
  enquête complète pour estimer ce qu'on rate.
- Risque restant : le pouvoir en place sera plus vérifié, quel qu'il soit. C'est
  assumé, et il faut l'écrire dans la méthodologie publique.
- Pluralisme faible : liste = titulaires de fonctions (registres officiels, Wikidata)
  plus les 50 comptes à plus forte audience du pays, sans tri éditorial. L'audience
  capte l'opposition, les exilés et les médias indépendants. En couverture C, notes de
  contexte seulement.

## Q2. Micro-lots

Lot = 10 posts ou 2 500 tokens de contenu, au premier atteint ; plafond dur à 12. N3
et N4 restent à 1, pour la latence.

- Levier oublié : raccourcis le préfixe de 1 500 à 900 tokens. Un lot de 10 tombe à
  340 tokens par post, presque autant qu'un lot de 20 aujourd'hui (325), sans le
  risque.
- Budget : avec le préfixe actuel, masse (1,4 M), N3 en solo (0,5 M) et rejeu
  (0,13 M) font 2,0 M sur 2,5 M, avant E5 et l'arbitrage. Avec 900 tokens, on descend à
  environ 1,65 M.
- Composition : mélange, et maximise la dissemblance. Jamais deux posts du même récit
  ni de similarité supérieure à 0,6 dans un lot. Entre posts éloignés, un chiffre
  étranger se voit ; entre posts proches, il passe le contrôle.
- Fidélité, dans les deux sens : tout chiffre ou entité de l'affirmation doit exister
  dans son post (contamination). À l'inverse, un post dont aucun chiffre n'apparaît
  dans une affirmation est rejoué seul (omission probable).
- Schéma : chaque identifiant d'entrée doit ressortir, avec des affirmations ou un
  motif d'absence fermé (opinion, question, satire…). Un identifiant manquant relance
  le post.
- Mesure des omissions : 2 % des posts rejoués seuls, soit environ 70 par jour et 500
  par semaine, pour 125 k tokens par jour. Cela estime un taux de 3 % à ±1,5 point.
  Stratifie par position dans le lot.
- Réglage automatique : si le dernier tiers du lot omet 1,5 fois plus que le premier
  sur deux semaines, la taille baisse de 2.
- Injection : oui, traitement seul. Ajoute deux règles : un lot avec au moins 2 échecs
  de fidélité est rejoué entièrement en solo, et un post canari synthétique entre dans
  1 lot sur 100.
- Risque restant : le rejeu solo n'est pas une vérité terrain. Les discordances, peu
  nombreuses, vont à un humain.

## Q3. Contradicteur et confiance

Remplace l'objection systématique par un second avis aveugle, et ne demande une
objection qu'en cas de désaccord.

Un modèle à qui on demande « la meilleure objection » en trouve toujours une, et il
est ancré par le verdict qu'on lui montre. L'accord de deux jugements indépendants est
un signal de confiance bien plus fort que « aucune objection trouvée ».

- Déroulé : le modèle OpenAI reçoit l'affirmation cadrée et les passages, dans un
  ordre différent, sans le verdict, et rend sa propre issue. Le backend compare.
- Accord de fond : même côté (Vrai ou Plutôt vrai, contre Trompeur ou Faux). Un cran
  d'écart du même côté est un écart de style, journalisé pour mesurer le décalage
  entre fournisseurs, sans effet sur la confiance.
- Désaccord de fond : changement de côté, ou verdict contre abstention. Un second
  appel demande alors une objection typée.
- 1. Types : oui, liste fermée, avec identifiant de passage obligatoire : preuve mal
  lue, preuve insuffisante pour l'issue, source non indépendante, période ou périmètre
  erronés, définition différente, cadrage infidèle à l'affirmation. L'objection
  s'affiche au relecteur comme point à vérifier en priorité.
- 2. Autre issue : pas dans l'objection. Seulement une direction fermée : plus
  sévère, moins sévère, abstention.
- 3. Grille : écris-la comme une régression logistique à coefficients posés à la main
  (inventés, non mesurés). La bascule devient continue : à 200 revues tu réestimes
  l'ordonnée et les 3 ou 4 coefficients les plus forts avec rétrécissement vers ces
  valeurs, puis tout le modèle à partir de 100 refus.
  - Ordonnée : 0.
  - Source primaire directe : +1,0.
  - 2 origines indépendantes : +0,5 ; 3 ou plus : +0,8.
  - Sources contradictoires : −1,5.
  - Second avis du même côté : +0,8 ; issue identique : +1,1 ; désaccord de fond :
    −1,5.
  - Seconde passe concordante : +0,4 ; discordante : −1,2.
  - Statistique avec fiche : +0,5 ; situation en cours : −0,8 ; causalité : −1,0 ;
    interprétation juridique : −0,5.
  - Issue « Trompeur » : −0,4. Couverture B : −0,3.
  - Confiance = sigmoïde de la somme, plafonnée à 0,90 avant calibration. Haute à
    partir de 0,80, moyenne à partir de 0,55.
- 4. Seconde passe : non en systématique. Le second avis reçoit déjà les passages dans
  un autre ordre, ce qui couvre le biais de position gratuitement. Garde-la sur
  désaccord, et toujours en N4.
- Quota : environ 18 avis à 7 k tokens plus 6 objections, soit 175 k sur 250 k.
- Risque restant : deux fournisseurs peuvent partager la même erreur sur un sujet mal
  couvert par les sources.

## Q4. Récits

Sépare les deux fonctions du récit. Compter et détecter tolère un regroupement lâche ;
appliquer un verdict exige un regroupement strict, déjà assuré par la signature. Le
récit partage un dossier de preuves.

- 1. Rattachement : au moins deux signaux, dont un non sémantique, et au moins une
  ancre spécifique partagée : entité rare (pondération par rareté sur 30 jours), média
  non générique ou lien. « Immigration » n'est pas une ancre, « Calais » en est une.
  - La co-occurrence compte si les deux affirmations apparaissent ensemble dans 3
    posts ou plus de 3 comptes distincts.
  - Un média vu dans de nombreux récits sans rapport (mème, logo) est exclu.
- 2. Bornes : clôture après 7 jours sans activité. Une résurgence crée un nouveau
  récit lié à l'ancien, qui hérite du dossier. Scission proposée au-delà de 25
  groupes, ou quand deux sous-ensembles ne tiennent que par un seul groupe.
- 3. Centroïde : ni figé ni recalculé. Utilise 1 à 3 médoïdes, des affirmations
  réelles : le fondateur, fixe, plus les deux groupes à plus forte portée, réélus au
  plus une fois par jour. Tout membre doit être proche d'un médoïde.
- 4. Héritage : insuffisant. Signaler chaque variante incompatible sature tes 12
  relectures par jour. Les variantes sont jugées à bas coût sur le dossier de preuves
  existant, façon E5, et seules celles à forte portée montent au relecteur.
- Risque restant : une ancre partagée par hasard, comme un nom de ville dans deux
  affaires distinctes. La réversibilité des fusions le couvre.

## Q5. Détection de vague

- Score : remplace le z-score par un écart de type Poisson avec plancher,
  (observé − attendu) ÷ √(attendu + 5). Garde des planchers absolus pour toujours,
  comme condition nécessaire.
- 1. Bascule : seuils absolus jusqu'à 1 000 récits clos. Ensuite, quantiles globaux
  (« top 1 % des récits au même âge »). Quantiles par thème à partir de 300 récits
  dans le thème, avec rétrécissement vers le global.
- 2. Normalisation : oui, mais par la part du flux observé plutôt que par
  installation. Cela neutralise la croissance de la base, le cycle jour-nuit et
  l'arrivée de plateformes, sans jeton d'installation.
  - Veille : 1 % du flux de l'heure et 10 comptes distincts.
  - Vague : 3 %, 30 comptes et doublement sur l'heure précédente.
  - Alerte : 8 % et 80 comptes, ou thème sous surveillance.
- Piège : tout changement des filtres serveur de l'extension ressemble à une vague.
  Versionne les filtres et gèle les bases 24 h après un changement.
- 3. Plateformes : « au moins 3 » est inutilisable au lancement. Règle indépendante du
  nombre de plateformes : une veille franchie séparément sur 2 plateformes vaut vague.
- Risque restant : deux vagues simultanées gonflent le dénominateur et s'atténuent
  l'une l'autre. Les planchers absolus limitent l'effet.

## Q6. Échantillonnage sous charge

Oui, mais le déclencheur est une nouveauté qui prend, pas une nouveauté vue une fois.

- Pourquoi : à 200 posts par minute, l'extraction épuiserait l'allocation du jour en
  40 minutes. Et une voie « nouveauté » sans garde est une cible : un attaquant génère
  des chiffres aléatoires pour vider le budget.
- Règle : empreinte de chaque nouveauté (chiffre normalisé, lien, mention, hachage de
  média, entité). Passage forcé quand une empreinte atteint 3 posts de 3 comptes
  distincts, ou un post au-dessus du 90e centile d'engagement du récit. Un seul
  exemplaire part en extraction : le post le plus engagé.
- Plafond : 20 % du budget de tokens du récit, classement par soutien × engagement.
- Résultat : le coût suit le nombre de variantes émergentes, quelques-unes par heure.
- Risque restant : la détection d'entités sans modèle est faible hors alphabet latin.
  Complète par la distance aux médoïdes.

## Q7. Identifiants

Table unique avec `platform` et `external_id TEXT` : oui. Partitionnement de cette
table : non.

- Volume : environ 7 M de lignes par an, ce qui ne justifie pas de partitionner.
- Coût caché : une table partitionnée impose la clé de partition dans toute clé
  unique, donc dans toutes les clés étrangères qui la référencent.
- Clé : identifiant interne `BIGINT` comme clé primaire, référencé partout.
  L'identifiant externe ne sert qu'aux frontières.
- Quoi partitionner : le journal d'observations, qui grossit et se purge.
- Colonne générée : utile seulement si tu tries par identifiant numérique. Sinon un
  index sur expression suffit. Si tu la gardes, crée-la avec la table et ajoute une
  contrainte de format (chiffres, borne `BIGINT`). Changer son expression plus tard
  réécrit la table. À vérifier sur ta version de PostgreSQL.
- Problème non posé, la RAM : si tu vectorises chaque post, 12 000 vecteurs par jour
  font environ 5 Go d'index par an, soit près de 50 $ par mois à ton tarif (ordre de
  grandeur). N'indexe que les groupes (environ 135 000 par an, 170 Mo), et garde les
  vecteurs de posts 14 jours dans une table partitionnée par semaine.

## Q8. Langue pivot

Option (a) aujourd'hui, pour une raison précise : le pivot doit être la langue
majoritaire du corpus. Un pivot anglais traduirait chaque affirmation française, soit
l'essentiel du volume, pour servir une minorité. Révise si le non-français dépasse
50 %.

- Vecteurs : texte d'origine pour les rapprochements dans une même langue. Vecteur
  pivot seulement pour les affirmations non françaises.
- Signature : extraite du texte d'origine, dans des champs neutres en langue
  (nombres, dates ISO, ISO 3166, polarité fermée, identifiant Wikidata pour le sujet
  quand il existe).
- Arbitre : systématique pour les paires interlingues, sur les deux textes d'origine.
  La comparaison des signatures ne suffit pas : sujet et prédicat restent du texte
  libre.
- Test : les paires multilingues de ClaimReview et le jeu de la tâche SemEval, sur
  200 paires, en comparant (a), (b) et (c).
- Risque restant : une négation perdue en traduction par un petit modèle. L'arbitre
  est là pour ça.

## Q9. Compatibilité des signatures

Ne réutilise pas de verdict entre variantes chiffrées : recalcule. Le verdict s'indexe
par fiche et bande de valeurs, pas par texte.

1. Intervalles : oui, tirés de la précision de l'affirmation. « 7 % » donne
   [6,5 ; 7,5[, qui contient 7,4 : l'arrondi d'usage est réglé sans paramètre.
2. Tolérance : elle ne sert qu'à graduer les intervalles qui ne contiennent pas la
   référence. τ = max(plancher, 5 % de la référence), avec un plancher de 0,2 point
   pour un taux.
3. Bandes : contenu ; à moins de 1 τ ; à moins de 3 τ ; au-delà ; avec le côté. Le
   relecteur valide une bande une fois (« au-dessus de 9 % : Faux »), et toute
   variante qui y tombe hérite.
4. Réutilisation directe : seulement à signature normalisée identique.
5. Période : deux affirmations sont compatibles si elles se résolvent vers la même
   fiche (indicateur, période, territoire, définition). « Actuellement » se résout à la
   date du post. Si la valeur correspond à la fiche précédente, tu détectes `outdated`
   sans modèle.
6. Champs à ajouter : la variante de définition (chômage au sens du BIT ou inscrits en
   catégorie A), la base de comparaison pour les affirmations relatives (« a doublé
   depuis 2017 ») et l'échelle d'unité.

- Risque restant : une erreur d'extraction de la signature. Mesure-la sur 50 paires
  pièges.

## Q10. Notes de contexte

1. Règles différentes : oui, trois régimes.
   - Note intrinsèque à l'objet (date d'une image, définition) : s'applique partout où
     l'objet apparaît et où la présentation le contredit.
   - Note relative à la formulation (`cherry_picking`, `causation`) : liée au groupe.
   - Note provisoire (« à cette heure ») : horodatée, expire en 6 h au plus sans
     revalidation, jamais héritée après.
2. Objet : oui. Types : média, indicateur, texte juridique avec version, événement,
   document cité, citation.
3. Taxonomie : je passerais à 15 types.
   - Ajouts : `manipulated_media`, `misrepresented_source` (l'étude ne dit pas cela),
     `developing` (ton exemple).
   - Élargissements : `truncated_quote` devient `quote_integrity` (tronquée, mal
     attribuée, inventée). `geography` devient `scope` (territoire ou population).
   - `satire` : je la garde, redéfinie en « origine satirique reprise au premier
     degré », liée à l'objet d'origine.

- Contradiction : les notes sur image supposent un hachage perceptuel dès
  l'ingestion, même si la lecture d'images vient plus tard.
- Risque restant : une note provisoire périmée devient fausse. D'où l'expiration
  courte.

## Q11. Sources partie prenante

1. Règle fermée : remplace le jugement par une jointure. Une source est partie
   prenante si son éditeur, ou une entité qui le contrôle selon le registre
   (`controlled_by`), figure dans l'affirmation comme auteur de la conduite évaluée,
   accusateur ou accusé, et si l'affirmation est de type bilan, conduite ou
   accusation.
   - Les entités sont extraites au cadrage, avant la collecte.
   - La règle est symétrique : gouvernement, opposition, entreprise, ONG, syndicat.
2. Indépendance par origine : le juge extrait pour chaque passage l'entité à qui
   l'information est attribuée. Deux sources de même origine comptent une fois, comme
   cette origine. Le domaine ne sert que de repli : un même domaine peut héberger des
   éditeurs d'indépendance très différente.
3. Chiffres officiels : ni l'une ni l'autre option, mais deux cas.
   - Statistique produite sous indépendance statutaire : acceptée seule. L'attribut du
     registre s'appuie sur des critères externes plutôt que sur ton jugement (adhésion
     aux normes de diffusion du FMI, revues par les pairs d'Eurostat : candidats à
     vérifier).
   - Auto-déclaration d'un bilan : primaire pour l'attribution seulement. Sans
     corroboration, véracité « Non vérifiable » et note qui attribue le chiffre, avec
     celui de l'autre partie s'il existe.

- Risque restant : le registre des relations de contrôle est lui-même un choix humain.
  Versionne-le et publie-le.
