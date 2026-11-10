# Questions d'architecture pour Fable

Brief autonome, à transmettre tel quel à Fable. Il suppose que Fable connaît son propre
plan du 10 novembre 2026 (cascade E0 à E8, profils Masse, Viral, Ciblé, Critique),
mais pas le plan d'implémentation qui en découle.

---

## Contexte commun

**Projet.** Seat 12A, fact-checking et contexte sur les réseaux sociaux. Backend
« 12A Tower » : Python 3.13 asyncio, PostgreSQL comme seule infrastructure (données,
outbox d'événements avec LISTEN/NOTIFY, file de jobs en `SKIP LOCKED`, verrous
consultatifs), déploiement sur Railway en plusieurs processus sans état (API, workers
« hangar »). La RAM est facturée environ 10 $ par Go et par mois : il n'y a aucun
modèle local, et les embeddings passent par une API (`text-embedding-3-small`,
512 dimensions, stockés en `halfvec` avec pgvector). Aucune publication automatique :
chaque brouillon est relu par un humain.

**Ce qui a changé depuis ton plan.**

1. **Deux fournisseurs.** OpenAI couvre les petits rôles (extraction, arbitrage de
   paires, vérification légère, rédaction des requêtes), dans une allocation gratuite
   quotidienne (2,5 M de tokens pour les petits modèles et 250 k pour les grands au
   palier bas ; 10 M et 1 M aux paliers 3 à 5). En contrepartie, les entrées peuvent
   servir à entraîner les modèles d'OpenAI. Anthropic couvre le cadrage, le jugement
   et la rédaction : Sonnet 5.5 en Viral, Opus 5.5 en Ciblé et Critique. Le
   **contradicteur** est un grand modèle OpenAI, donc un autre fournisseur que le juge.
2. **Niveaux N0 à N4.** N0 Écarté, puis N1 Masse, N2 Viral, N3 Ciblé et N4 Critique,
   qui reprennent tes profils. Le niveau est porté par le groupe d'affirmations et
   pilote tous les réglages.
3. **Nouveaux étages.** Un étage de **cadrage** (E6, Anthropic) précède la collecte :
   ce que l'affirmation dit littéralement, ses sous-entendus, l'interprétation
   charitable, les définitions, la période, les conditions de vérité et de fausseté,
   les sous-questions. Un étage de **rédaction** (E9) suit le jugement, avec des
   contrôles déterministes.
4. **Deux axes de sortie.** Véracité (Vrai, Plutôt vrai, Trompeur, Faux, Non
   vérifiable) et **besoin de contexte** (aucun, utile, nécessaire), avec des types de
   contexte fermés : `outdated`, `recycled_media`, `cherry_picking`,
   `missing_baseline`, `truncated_quote`, `causation`, `definition`, `geography`,
   `procedure`, `scientific_uncertainty`, `satire`, `missing_event_context`. La note
   de contexte fait 280 caractères au plus, sur le modèle des notes de la communauté.
5. **Récits.** Les **récits** sont des groupes de groupes d'affirmations. Un **mode
   vague** se déclenche quand un récit accélère anormalement : une seule enquête sert
   tout le récit, en voie rapide.
6. **Couverture mondiale.** Chaque affirmation porte sa langue et ses juridictions
   (ISO 3166). Le français sert de langue pivot pour les embeddings. Les sources de
   chaque pays sont décrites dans des registres en base. Chaque pays a un niveau de
   couverture A, B ou C selon les sources ouvertes disponibles.
7. **Atlas.** Les sources structurées sont interrogées avant la recherche web : nos
   fiches de faits, ClaimReview, Wikidata, les statistiques publiques, le droit
   (copie locale des codes français avec dates de vigueur, EUR-Lex, Fedlex…),
   Wikipédia (révision figée, contrôle du risque d'annulation de la révision), puis
   l'actualité, puis la recherche web payante.
8. **Multi-plateforme.** X d'abord, puis Bluesky, Threads, etc. Les images et
   vidéos viendront plus tard.

**Hypothèses de volume** (les tiennes, toujours non confirmées) : 20 000 posts
observés par jour, environ 3 600 en extraction, environ 370 groupes nouveaux, 18
brouillons par jour pour environ 12 relus.

**Ce que j'attends de toi pour chaque question.** Une recommandation tranchée (oui,
non, ou quelle option), la raison principale, le risque restant et, si possible, une
règle ou un seuil concret qu'on peut coder. Si une question repose sur une hypothèse
fausse, dis-le.

---

## Q1. Le niveau Ciblé et la neutralité

**Contexte.** Le niveau N3 (Opus, contradicteur systématique, 8 recherches, verdict
en 1 h) s'applique quand l'auteur figure dans une liste de comptes suivis. La règle 10
du projet interdit qu'une logique de priorité ou de verdict dépende de l'auteur ou de
son bord.

**Ce qui est prévu.**
- Le niveau peut dépendre du **rôle public**, appliqué à l'identique dans tous les
  pays : chef d'État, ministre, parlementaire, candidat, parti, média, institution,
  seuil d'abonnés.
- Le bord est interdit partout.
- Le juge ne voit ni l'auteur, ni le niveau, ni l'engagement.
- La liste est publiée. Sa composition par bord est mesurée hors pipeline, dans une
  table séparée, lue par l'analytique seule.

**Le doute.** Le rôle public n'influence pas le verdict, mais il influence la
**profondeur** de la vérification. Un compte de N3 bénéficie d'une enquête plus
poussée, donc de verdicts plus fins et de plus de « Non vérifiable » quand les preuves
manquent. Un compte de N1 reçoit au mieux une vérification légère. Si la liste est
déséquilibrée, ou si un bord compte plus d'élus actifs sur X, la différence de
traitement devient une différence de résultats.

**Questions.**
1. Rôle public autorisé et bord interdit : cette règle suffit-elle, ou faut-il une
   contrainte supplémentaire ?
2. Si une contrainte supplémentaire est utile, laquelle choisis-tu ?
   - **(a)** des quotas de traitement par bord, mesurés a posteriori et corrigés ;
   - **(b)** une composition de liste contrainte, par exemple au prorata des sièges
     parlementaires ou des voix ;
   - **(c)** une exigence de verdict identique entre niveaux, où seule la latence
     changerait et non la profondeur ;
   - **(d)** autre chose.
3. Comment définir la liste pour des pays où le pluralisme est faible ?

---

## Q2. Micro-lots en extraction (E2)

**Contexte.** L'extraction tourne sur un petit modèle OpenAI. Avec un post par appel,
on consomme environ 1 750 tokens par post (1 500 d'instructions et 250 de contenu),
soit 6,3 M de tokens par jour : on dépasse l'allocation gratuite du palier bas. En
regroupant 20 posts par appel autour d'un préfixe d'instructions commun, on tombe à
environ 320 tokens par post, soit 1,15 M par jour.

**Ce qui est prévu.**
- Chaque post est délimité, identifié et marqué (« datamarking »).
- La sortie est un schéma strict indexé par l'identifiant du post.
- Un contrôle de fidélité vérifie que l'affirmation reste proche de son post :
  similarité d'embedding, et chiffres et entités du post retrouvés dans
  l'affirmation. En cas d'échec, le post est réessayé seul.
- Un test de contamination croisée fait partie du jeu de référence : un post piégé
  ne doit pas modifier les sorties des autres.
- Taille du lot : 10 à 20 en N1, 1 à 5 en N3 et N4.

**Le doute.** Un lot apporte trois risques :
- la contamination entre posts, avec des entités ou des chiffres qui passent d'un
  post à l'autre ;
- l'injection : un post hostile peut agir sur ses 19 voisins ;
- la baisse de qualité en fin de lot (attention diluée).

Le contrôle de fidélité détecte les fuites de chiffres et d'entités. Il ne détecte
pas une affirmation **omise**.

**Questions.**
1. Quelle taille de lot maximale recommandes-tu ?
2. Faut-il trier les lots, par thème pour l'homogénéité, ou au contraire mélanger
   aléatoirement pour éviter les fuites entre posts proches ?
3. Comment mesurer les omissions sans relire chaque post ? Je pense à un
   échantillon rejoué post par post et comparé au résultat du lot. Quelle taille
   d'échantillon ?
4. Un post qui contient des marqueurs d'injection connus doit-il être sorti du lot
   et traité seul ?

---

## Q3. Contradicteur d'un autre fournisseur et confiance

**Contexte.** Le juge est Anthropic : Sonnet en N2, Opus en N3. Le contradicteur est
un grand modèle OpenAI (gratuit dans l'allocation). Il reçoit le verdict et les
passages, et doit produire la meilleure objection ou déclarer qu'il n'en trouve pas.
La confiance n'est jamais déclarée par un modèle : elle est calculée en E10 à partir
de plusieurs signaux :
- le nombre et le niveau des sources ;
- l'accord entre les sources ;
- l'accord entre le juge et le contradicteur ;
- l'accord entre deux passes du juge, avec des passages dans un ordre différent
  (N3 sur désaccord, N4 toujours) ;
- le type d'affirmation.

La grille est fixe au départ, puis remplacée par une régression sur les décisions
des relecteurs à partir de 200 revues.

**Le doute.** Deux fournisseurs différents font des erreurs moins corrélées : c'est
le but. Mais ils divergent aussi sur le **style** (seuil entre « Trompeur » et
« Plutôt vrai », goût de la nuance). Le contradicteur pourrait objecter
systématiquement sur des points de forme, et la confiance baisserait sans raison.

**Questions.**
1. Le contradicteur doit-il produire une objection **typée** dans une liste fermée
   (preuve mal lue, source non indépendante, période erronée, définition différente,
   issue trop forte, issue trop faible, aucune objection), pour ne compter que les
   objections de fond ?
2. Faut-il le laisser proposer une autre issue, ou seulement objecter ?
3. Avant les 200 revues, quelle grille de confiance initiale proposes-tu ? Je pense
   à quelques règles pondérées simples ; donne des poids si tu peux.
4. La seconde passe du juge pour détecter le biais de position est-elle rentable
   (environ +7 $ par mois), ou le contradicteur suffit-il ?

---

## Q4. Récits : fusion sans fourre-tout

**Contexte.** Trois niveaux de regroupement :
- **copies** : SimHash 64 bits, distance de Hamming ≤ 3 ;
- **groupes d'affirmations** : similarité au-dessus d'un seuil et signature
  compatible ;
- **récits** : groupes qui racontent la même histoire avec des affirmations
  différentes. Exemple : « les migrants de Calais ont reçu des logements sociaux »,
  « la mairie a expulsé des familles pour les loger », et une photo de camp présentée
  comme récente.

Le rattachement d'un groupe à un récit est **incrémental** (pas de regroupement
global nocturne, que tu avais écarté pour son instabilité). Il s'appuie sur la
proximité du centroïde dans une fenêtre de temps, la co-occurrence (mêmes posts ou
mêmes comptes), les entités partagées et les médias partagés. Fusions et scissions
sont journalisées et réversibles.

**Le doute.**
- Avec des seuils trop lâches, on obtient des récits fourre-tout (« tout ce qui
  parle d'immigration ») : une enquête unique n'a plus de sens, et un verdict
  appliqué au récit devient faux pour certaines variantes.
- Avec des seuils trop stricts, une rumeur qui **mute** (chiffre qui grossit, lieu
  qui change, nouvel angle) se fragmente en dix récits, et on perd le mode vague.

**Questions.**
1. Quelle règle de rattachement recommandes-tu ? Faut-il exiger au moins **deux**
   signaux sur quatre (sémantique, co-occurrence, entité, média) plutôt qu'un seul ?
2. Faut-il borner un récit dans le temps (fenêtre de 7 jours sans nouvelle activité,
   puis clôture) et en taille (au-delà de N groupes, scission proposée au relecteur) ?
3. Le centroïde dérive à mesure que des groupes s'ajoutent. Faut-il le figer sur le
   groupe fondateur, ou le recalculer ?
4. Quand un verdict est appliqué à un récit, la règle prévue est : seuls les groupes
   à signature compatible héritent du verdict, les autres sont signalés au
   relecteur. Est-ce suffisant ?

---

## Q5. Détection de vague sans historique

**Contexte.** L'ordonnanceur calcule toutes les 2 minutes, par récit, la vélocité,
l'accélération, la diversité (comptes, installations de l'extension, plateformes) et
la portée, sur des fenêtres de 5 min, 1 h et 6 h. Le score est un z-score robuste
(médiane et écart absolu médian) sur l'historique du thème. Paliers indicatifs :
veille à z ≥ 3, vague à z ≥ 5 ou sur au moins 3 plateformes, alerte à z ≥ 8 ou sur un
thème sous surveillance.

**Le doute.**
- Au lancement, il n'y a aucun historique : le z-score est instable.
- Les volumes observés dépendent de notre propre couverture (nombre d'installations
  de l'extension, comptes suivis) et pas seulement de la viralité réelle. Une hausse
  du nombre d'installations ressemblerait à une vague.

**Questions.**
1. Pendant combien de temps (ou combien d'observations) faut-il utiliser des seuils
   absolus avant de basculer sur le z-score ? Quels seuils absolus de départ ?
2. Faut-il normaliser la vélocité par notre couverture du moment, par exemple en
   posts par installation active, pour neutraliser la croissance de la base
   d'utilisateurs ?
3. La diversité des plateformes (≥ 3) est-elle un bon signal fort, sachant que les
   autres plateformes arriveront progressivement ?

---

## Q6. Échantillonnage sous charge pendant une vague

**Contexte.** Au-delà d'un débit par récit (par exemple 200 posts par minute), seul
un échantillon aléatoire stratifié par plateforme et par heure passe en extraction.
Le reste est compté, pour que la portée reste juste. Les copies exactes et les
quasi-doublons héritent déjà des affirmations sans appel de modèle.

**Le doute.** L'échantillonnage aléatoire peut rater **la** variante qui change tout :
un nouveau chiffre, une nouvelle accusation, une vidéo. Or c'est souvent elle qui
relance la vague.

**Question.** Faut-il forcer le passage en extraction, hors échantillon, des posts
qui :
- s'éloignent du centroïde du récit au-delà d'un seuil ;
- ou contiennent un chiffre ou une entité jamais vus dans le récit ;
- ou portent un média nouveau ?

Si oui, quel plafond pour éviter que cette voie de contournement ne réintroduise le
flot ?

---

## Q7. Identifiants multi-plateformes

**Contexte.** Règle du projet : les identifiants X sont stockés en `BIGINT` et
exposés en chaînes dans l'API. Les autres plateformes ont des formats différents
(URI `at://` pour Bluesky, chaînes pour YouTube).

**Ce qui est prévu.**
- Une table unique `content_items` avec `platform` (CHECK) et `external_id TEXT`,
  plus `UNIQUE (platform, external_id)`.
- Une colonne générée stockée `x_post_id BIGINT`, qui vaut `external_id::bigint`
  quand `platform = 'x'`, avec un index partiel.

**Options écartées.**
- Une table par plateforme : jointures et pipeline à dupliquer.
- Une colonne `BIGINT` seule : impossible pour Bluesky.

**Question.** Ce compromis est-il le bon pour une table qui grossira de plusieurs
dizaines de milliers de lignes par jour et sera partitionnée par mois ? Vois-tu un
problème avec une colonne générée sur une table partitionnée, ou avec les migrations
expand/contract à venir ?

---

## Q8. Langue pivot pour les embeddings

**Contexte.** Le périmètre est mondial. L'extraction produit, **dans le même appel**,
l'affirmation dans la langue du post (`text_original`) et une traduction française
(`text_fr`). Les embeddings sont calculés sur `text_fr`, pour qu'une rumeur traduite
en dix langues forme un seul groupe. La tâche SemEval-2025 n° 7 indique que traduire
vers une langue pivot puis vectoriser vaut à peu près les modèles multilingues sur
les textes d'origine. Les citations restent vérifiées contre le texte original des
sources, jamais contre une traduction.

**Options.**
- **(a)** Pivot français (langue de l'équipe, relecture directe).
- **(b)** Pivot anglais (langue dominante des modèles et des données
  d'entraînement des embeddings, probablement meilleure séparation).
- **(c)** Embedding de la langue d'origine avec le modèle multilingue, sans pivot.

**Le doute.** Avec un pivot, une erreur de traduction (nuance, négation, unité) peut
fusionner deux affirmations différentes. C'est précisément l'erreur que la signature
structurée doit empêcher.

**Questions.**
1. Quelle option recommandes-tu ?
2. La comparaison déterministe des signatures suffit-elle à neutraliser les erreurs
   de traduction, ou faut-il un arbitre systématique pour les paires interlingues ?

---

## Q9. Règles de compatibilité des signatures (réutilisation d'un verdict)

**Contexte.** C'est le cœur de ta règle « la similarité ne suffit jamais ». La
signature compte sept champs : sujet, prédicat ou mesure, valeur et unité, période,
lieu, polarité, source invoquée. J'ajoute la monnaie, la date de valeur, le type
d'affirmation et les juridictions. La cible est un taux de fausse réutilisation
inférieur à 1 %.

**Le doute.** Ta formulation laisse ouverte la règle de compatibilité des
**valeurs**, sur trois cas :
- « Le chômage est à 7,4 % », « à 7 % », « à presque 8 % », « à 9 % » : où passe
  la frontière entre même verdict et verdicts différents ?
- Une valeur approximative (« presque », « plus de », « environ ») doit-elle être
  traduite en intervalle ?
- Une période implicite (« actuellement ») doit-elle être compatible avec « T3
  2026 » ?

**Questions.** Peux-tu proposer une règle codable ? Par exemple :
1. valeurs comparées par **intervalles** (« presque 8 » donne [7,5 ; 8[, « plus de
   10 000 » donne ]10 000 ; +∞[) ;
2. verdict réutilisé seulement si l'intervalle de la variante tombe **entièrement**
   du même côté de la valeur de référence que la variante déjà jugée ;
3. sinon, on ne réutilise que la fiche de référence, et E5 juge la variante.

Comment traiter les arrondis d'usage (« 7 % » pour 7,4 %) : tolérance relative fixe,
ou dépendante du type d'indicateur ?

---

## Q10. Notes de contexte appliquées à un récit

**Contexte.** En mode vague, le premier brouillon est souvent une note de contexte
prudente (« L'image date de 2019… », « Aucune source officielle ne confirme à cette
heure… »), avant un verdict complet. Une fois validée, la note s'applique aux groupes
compatibles du récit.

**Questions.**
1. Une note de contexte doit-elle obéir à des règles de réutilisation **différentes**
   de celles d'un verdict ? Une note sur la date d'une image s'applique à tous les
   posts qui portent cette image, quel que soit le texte. Une note sur une définition
   (chômage au sens du BIT) s'applique à toutes les affirmations sur cet indicateur.
2. Faut-il rattacher une note à un **objet** (média, indicateur, texte de loi) plutôt
   qu'à un groupe ?
3. La taxonomie à 12 types de contexte te paraît-elle complète et sans
   chevauchement ? Que fusionnerais-tu ou ajouterais-tu ?

---

## Q11. Indépendance des sources et sources partie prenante

**Contexte.** Ta règle : « Vrai » et « Faux » exigent deux sources indépendantes dont
une primaire. Ce qui est prévu pour la rendre opérationnelle :
- deux passages quasi identiques (même SimHash), par exemple une dépêche reprise
  par dix journaux, comptent pour **une** source ;
- deux sources du même domaine enregistrable comptent pour une ;
- une source **partie prenante** de l'affirmation ne compte pas comme source
  indépendante. Par exemple, un gouvernement qui défend sa propre action dans un
  conflit : il reste primaire pour ses statistiques officielles, mais pas preuve
  indépendante de son propre bilan. Le champ `party_to_claim` est évalué au cadrage.

**Le doute.**
- La qualification de « partie prenante » est elle-même un jugement, donc un point
  d'entrée de biais.
- Pour certaines affirmations, la seule source primaire est la partie prenante (un
  ministère sur ses propres chiffres) : la règle conduirait à l'abstention
  systématique.

**Questions.**
1. Comment formuler la règle « partie prenante » de façon assez fermée pour
   qu'elle s'applique de la même manière à tous les pays et à tous les bords ?
2. Pour les chiffres officiels produits par la partie concernée, faut-il :
   - accepter la source primaire seule, assortie d'une mention de contexte ;
   - ou exiger une source indépendante (Cour des comptes, organisme statistique
     indépendant, organisation internationale) ?
