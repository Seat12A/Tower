# Pipeline : étages et intégration dans Tower

Douze étages transforment un flux de contenus en quelques brouillons relus par jour.
Seuls E2, E3 (zone grise), E5, E6, E8 et E9 appellent un modèle, et seuls E6, E8 et E9
appellent Anthropic. Les volumes sont ceux du scénario supposé (20 000 posts observés
par jour), à corriger quand les volumes réels seront connus.

## Vue d'ensemble

```
 content.observed ─▶ E0 Normalisation ─▶ E1 Routage ─▶ E2 Extraction ─▶ E3 Appariement
   (ingest)           code, empreintes    embeddings     OpenAI petit      pgvector + signature
                                          + classifieur  (micro-lots)      + fact-checks existants
                                                                              │
         ┌────────────────────────────────────────────────────────────────────┘
         ▼
   E4 Ordonnanceur ─▶ E5 Vérif. légère ─▶ E6 Cadrage ─▶ E7 Collecte ─▶ E8 Jugement
   code, budget,      Atlas niveaux 1-3   Sonnet/Opus   backend, Atlas   juge Anthropic
   niveaux, vagues    OpenAI petit        « recul »     + web            + second avis OpenAI 
                                                                              │
         ┌────────────────────────────────────────────────────────────────────┘
         ▼
   E9 Rédaction ─▶ E10 Assemblage ─▶ E11 Revue et apprentissage
   brouillon +      confiance, contexte    Discord, corrections,
   note de contexte du compte, revérif     étiquettes d'apprentissage
   contrôles déterm.
```

Correspondance avec le plan de Fable : E0 à E5 inchangés ; E6 (cadrage) est nouveau ;
E7 et E8 découpent l'ancien E6 (collecte, puis jugement) ; E9 (rédaction et contrôles)
est nouveau ; E10 = ancien E7 ; E11 = ancien E8.

## Étages

### E0 — Normalisation et dédoublonnage (code seul)

| | |
|---|---|
| Entrée | `content_item` observé (toute plateforme) |
| Traitement | Normalisation Unicode (NFKC), minuscules, suppression des URL de suivi, des mentions (remplacées par `@user`) et des emojis décoratifs. Détection de langue légère (heuristique sur mots outils, sans modèle). Comptage des mots utiles. Empreinte exacte (SHA-256 du texte normalisé) et **SimHash 64 bits** pour les quasi-copies. |
| Passe si | Langue dans la liste des langues prises en charge (configurable, élargie progressivement : voir [international.md](international.md)), au moins 8 mots utiles, pas une copie exacte d'un contenu déjà traité. Une copie hérite des affirmations de l'original. |
| Volume | 20 000 → 12 000 |
| Coût | nul |

Le SimHash est stocké en `BIGINT` et indexé par bandes de 16 bits (quatre colonnes
générées) : une recherche de quasi-copie à distance de Hamming ≤ 3 est un simple
`SELECT` sur index B-tree, sans RAM dédiée ni embedding. Il détecte copier-coller,
« copypasta » et retweets avec ajout mineur, très fréquents dans les vagues de rumeurs.

### E1 — Routage par embeddings (code + API d'embeddings)

| | |
|---|---|
| Entrée | Post unique |
| Traitement | Embedding OpenAI `text-embedding-3-small` tronqué à 512 dimensions (par lots de 100 à 2 000 textes par requête). Score « vérifiable » par une régression logistique dont les poids sont en base, calculée **en Python pur** (512 multiplications : aucun numpy en production). |
| Passe si | Score au-dessus du seuil du niveau. |
| Volume | 12 000 → 3 600 |
| Coût | environ 1 M de tokens par jour, soit 0,02 $ par jour |

Les vecteurs de posts ne sont **pas indexés** (un index HNSW sur 12 000 vecteurs par
jour atteindrait plusieurs Go par an, voir [reponses-fable.md](reponses-fable.md),
Q7). Ils sont conservés 14 jours dans une table partitionnée par semaine
(`content_embeddings`), pour le classifieur, l'entraînement et les distances aux
médoïdes d'un récit (calculs exacts sur de petits ensembles). Le dédoublonnage au
niveau du post repose sur le SimHash (E0) ; le regroupement sémantique se fait au
niveau des affirmations et des groupes (E3), dont seuls les **groupes** sont indexés.

Au démarrage, sans données, le classifieur est remplacé par une règle permissive (tout
passe à E2) : c'est le scénario « démarrage à froid » de [couts.md](couts.md). Le
classifieur est **distillé** chaque semaine à partir des décisions de E2 et des
relecteurs (`tower factcheck train-router`, exécuté hors des réplicas de production) ;
2 % des posts écartés sont tirés au hasard et envoyés à E2 pour mesurer en continu le
taux de faux négatifs.

### E2 — Extraction (OpenAI petit modèle, micro-lots)

| | |
|---|---|
| Entrée | N1 : lot de 10 posts ou de 2 500 tokens de contenu, au premier atteint (plafond dur : 12). N3/N4 : 1 post par appel (latence). Chaque post est délimité, identifié et marqué, avec le texte du post parent ou cité tronqué à 280 caractères |
| Sortie (schéma strict) | **Chaque identifiant d'entrée ressort**, avec des affirmations ou un motif d'absence fermé (`opinion`, `question`, `satire`, `no_claim`, `unreadable`) ; un identifiant manquant relance le post. Par post : liste d'affirmations canoniques (≤ 300 caractères, formulées de façon neutre, dans la langue du post **et** en français, langue pivot), juridictions (codes pays), signature à sept champs avec valeur normalisée (unité, monnaie, échelle), type de péremption, thème, vérifiabilité estimée (0-1), indices de contexte (voir [contexte.md](contexte.md)), modalité source (texte, image, vidéo) |
| Contrôle | Fidélité **dans les deux sens**. Contamination : tout chiffre ou entité de l'affirmation doit exister dans son post, sinon l'affirmation est rejetée et le post rejoué seul. Omission : un post dont aucun chiffre n'apparaît dans une affirmation est rejoué seul. Un lot avec au moins 2 échecs de fidélité est rejoué entièrement en solo. |
| Volume | 3 600 posts → environ 2 200 affirmations |
| Modèle | rôle `extract`, voir [modeles.md](modeles.md) |

**Seul E2 et E9 lisent le texte brut d'un post** (voir [biais.md](biais.md) pour la
défense contre l'injection).

Règles des micro-lots (arbitrées avec Fable, [reponses-fable.md](reponses-fable.md),
Q2) :

- **Préfixe d'instructions court** : 900 tokens au plus (au lieu de 1 500). Un lot de
  10 revient à environ 340 tokens par post, presque autant qu'un lot de 20 avec
  l'ancien préfixe, sans le risque.
- **Composition par dissemblance** : jamais deux posts du même récit ni deux posts de
  similarité supérieure à 0,6 dans un même lot. Entre posts éloignés, un chiffre
  étranger se voit ; entre posts proches, il passerait le contrôle.
- **Injection** : un post qui contient des marqueurs d'injection connus est traité
  seul. Un post **canari** synthétique entre dans 1 lot sur 100 pour vérifier en
  continu l'absence de contamination.
- **Mesure des omissions** : 2 % des posts (environ 70 par jour) sont rejoués seuls et
  comparés au résultat du lot, avec un échantillon stratifié par position dans le
  lot. Cela estime un taux d'omission de 3 % à ±1,5 point par semaine. Les
  discordances vont à un humain (le rejeu solo n'est pas une vérité terrain).
- **Réglage automatique** : si le dernier tiers des lots omet 1,5 fois plus que le
  premier tiers sur deux semaines, la taille des lots baisse de 2.

Signature : les sept champs du plan de Fable (sujet, mesure ou prédicat, valeur et
unité, période, lieu, polarité, source invoquée), **extraits du texte d'origine dans
des champs neutres en langue** : nombres, dates ISO, codes ISO 3166, polarité fermée,
identifiant Wikidata du sujet quand il existe. Champs ajoutés :

- `claim_type` : statistique, fait historique, citation, texte juridique, situation en
  cours, procédure, bilan, conduite, accusation, prédiction, opinion ;
- `jurisdictions` : une affirmation peut concerner plusieurs pays (voir
  [international.md](international.md)) ;
- `value_low`, `value_high` : **intervalle** tiré de la précision de l'affirmation
  (« 7 % » donne [6,5 ; 7,5[, « presque 8 % » donne [7,5 ; 8[, « plus de 10 000 »
  donne ]10 000 ; +∞[) ;
- `unit_scale`, `currency`, `value_date` ;
- `definition_variant` (chômage au sens du BIT ou inscrits en catégorie A) ;
- `comparison_base` pour les affirmations relatives (« a doublé depuis 2017 ») ;
- `entities` : entités citées, avec leur rôle (auteur de la conduite évaluée,
  accusateur, accusé), utilisées par la règle des sources partie prenante (E7).

### E3 — Appariement (pgvector + signature + arbitre)

| | |
|---|---|
| Entrée | Affirmation canonique et son embedding |
| Traitement | 1) Recherche des groupes les plus proches (pgvector sur les groupes, top 5 ; vecteur du texte d'origine pour une même langue, vecteur pivot français pour les autres langues). 2) Comparaison **déterministe** des signatures normalisées. 3) Résolution vers une **fiche de référence** (indicateur, période, territoire, définition) quand l'affirmation est chiffrée. 4) Arbitre petit modèle OpenAI en zone grise, et **systématiquement pour les paires interlingues** (sur les deux textes d'origine). 5) Recherche parallèle dans les **fact-checks existants** (ClaimReview, nos verdicts publiés) par embedding et mots-clés. |
| Sortie | Rattachement à un groupe existant ou création d'un groupe ; rattachement du groupe à un récit (voir [vagues.md](vagues.md)) ; lien vers un fact-check externe trouvé. |
| Volume | 2 200 → environ 370 groupes nouveaux |

**Réutilisation d'un verdict** (règle de Fable, [reponses-fable.md](reponses-fable.md),
Q9) :

1. **Directe** seulement à signature normalisée **identique**.
2. **Entre variantes chiffrées, jamais de réutilisation par similarité** : le verdict
   s'indexe par fiche de référence et **bande de valeurs**. On compare l'intervalle de
   la variante à la valeur de référence de la fiche, avec une tolérance
   τ = max(plancher, 5 % de la référence) ; plancher de 0,2 point pour un taux.
   Bandes : intervalle qui contient la référence ; à moins de 1 τ ; à moins de 3 τ ;
   au-delà ; chacune avec son côté (au-dessus ou en dessous).
3. Un relecteur **valide une bande une fois** (« au-dessus de 9 % : Faux ») ; toute
   variante qui tombe dans une bande validée en hérite. Sinon, la vérification légère
   E5 juge la variante sur la fiche.
4. **Période** : deux affirmations sont compatibles si elles se résolvent vers la même
   fiche. « Actuellement » se résout à la date du post. Si la valeur correspond à la
   fiche **précédente** de l'indicateur, le type de contexte `outdated` est détecté
   sans modèle.

Le risque restant est une erreur d'extraction de la signature : il est mesuré sur 50
paires pièges du jeu de référence.

### E4 — Ordonnanceur (code seul)

Job exécuté en singleton par verrou consultatif (`pg_try_advisory_xact_lock`), comme
la maintenance du socle. **Sa cadence s'adapte au flux** (demande de Jules : au départ,
une dizaine de tweets par jour ; la charge peut monter vite) :

- **réveil par événement** : un contenu N3 ou N4 (compte suivi, forçage) est traité
  dès son arrivée, sans attendre le prochain passage ;
- **passage périodique** dont l'intervalle suit le débit d'entrée mesuré sur l'heure
  écoulée, pour viser environ 5 nouveaux contenus par passage :
  `intervalle = borne(60 s, 30 min, 5 ÷ débit)`. À 10 tweets par jour, un passage
  toutes les 30 minutes ; à 20 000 par jour, toutes les minutes ou presque ;
- **lots qui attendent** : un lot d'extraction N1 part dès qu'il atteint 10 posts, ou
  après un délai maximal (6 h au départ), pour ne pas garder un post isolé
  indéfiniment ;
- les bornes et la cible sont dans `processing_profiles` : aucun changement de code
  quand le volume grandit.

À chaque passage, il :

1. recalcule portée, vélocité et niveau des groupes actifs (agrégats SQL, pas de
   chargement en mémoire) ;
2. applique les promotions et rétrogradations ([niveaux.md](niveaux.md)) ;
3. détecte les vagues ([vagues.md](vagues.md)) ;
4. classe les groupes en attente par score de priorité ;
5. réserve le budget et enfile les jobs E5 ou E6 dans la limite du budget, du quota
   de brouillons (50 par jour au plus, voir [niveaux.md](niveaux.md)) et de la capacité
   des files.

Volume (scénario de 20 000 posts par jour) : 370 → environ 75 groupes par jour qui
avancent. Au démarrage (une dizaine de tweets par jour), tout ce qui est vérifiable
peut avancer.

### E5 — Vérification légère (Atlas + petit modèle)

| | |
|---|---|
| Entrée | Affirmation canonique du groupe |
| Traitement | Atlas niveaux 1 à 3 : fiches de référence, fact-checks existants, source structurée du type (Wikidata, série INSEE ou Eurostat, article de code). Au plus 1 à 2 recherches web en N1. Petit modèle OpenAI : compare l'affirmation aux éléments trouvés. |
| Sortie | Signal préliminaire (`likely_true`, `likely_false`, `uncertain`, `needs_context`), fiche de référence candidate, éléments trouvés |
| Passe si | Signal `likely_false`, `uncertain` ou `needs_context`, portée suffisante, quota non atteint |
| Volume | 75 → 18 |

Ce signal n'est **jamais publié** : il ne sert qu'à prioriser. Quand une fiche de
référence existe, E5 tranche une variante chiffrée par simple comparaison
(environ 0,0003 $ ou gratuit).

### E6 — Cadrage : prendre du recul (Anthropic)

Étage nouveau. Avant de chercher des preuves, le modèle établit **ce qu'il faut
vérifier**. Sortie structurée (détail dans [contexte.md](contexte.md)) :

- ce que l'affirmation affirme littéralement, et ses sous-entendus ;
- l'interprétation la plus charitable et la plus forte (principe de charité) ;
- les définitions en jeu (ex. chômage au sens du BIT ou demandeurs d'emploi
  France Travail) ;
- la période et le territoire de référence ;
- ce qui rendrait l'affirmation vraie, et ce qui la rendrait fausse ;
- les pièges connus du type (chiffre absolu ou relatif, base de comparaison, période
  choisie, corrélation et causalité) ;
- les sous-questions à vérifier et le type de source attendu pour chacune ;
- la décision « vérifiable ou non » motivée (opinion, prédiction → issue
  « Non vérifiable » immédiate, sans enquête).

Le cadrage ne reçoit que l'affirmation canonique et les indices de contexte d'E2, pas
le post ni l'auteur.

### E7 — Collecte hors modèle (backend + Atlas)

1. Un petit modèle OpenAI rédige les requêtes **à partir du cadrage**, jamais du
   tweet : requêtes symétriques (qui confirmeraient et qui infirmeraient), requêtes par
   type de source.
2. Le backend interroge l'Atlas dans l'ordre de résolution ([connaissances.md](connaissances.md)),
   puis la recherche web si nécessaire, dans la limite du niveau.
3. Récupération sûre des pages (port `WebFetcher` : taille plafonnée à 2 Mo, types
   autorisés, protection SSRF, respect de `robots.txt` et des réservations de fouille).
4. Extraction du texte, découpage en passages d'environ 120 mots, pré-filtre lexical
   PostgreSQL puis sélection des 50 meilleurs passages par embeddings. Les passages
   quasi identiques (SimHash) sont fusionnés.
5. Chaque passage reçoit un identifiant `P1…Pn`, son éditeur, son domaine, son niveau
   de fiabilité, sa date, l'empreinte SHA-256 de la page.
6. **Sources partie prenante** (règle fermée, [reponses-fable.md](reponses-fable.md),
   Q11) : une source est partie prenante si son éditeur, ou une entité qui le contrôle
   selon le registre (`controlled_by`), figure dans l'affirmation comme auteur de la
   conduite évaluée, accusateur ou accusé, **et** si l'affirmation est de type bilan,
   conduite ou accusation. C'est une jointure entre les entités extraites au cadrage
   et le registre, pas un jugement de modèle. La règle est symétrique : gouvernement,
   opposition, entreprise, ONG, syndicat.

Budget de lecture du juge : 4 000 à 6 000 tokens de passages (jusqu'à 12 000 en N4).

### E8 — Jugement (juge Anthropic + second avis OpenAI)

**Profondeur uniforme, escalade par difficulté** (arbitrage de Fable,
[reponses-fable.md](reponses-fable.md), Q1). Toute enquête, en N2 comme en N3, démarre
à la **même profondeur** : juge Sonnet 5.5, 4 recherches, second avis. Elle passe en
**profondeur renforcée** (juge Opus 5.5, 4 recherches de plus, seconde passe du juge)
si l'une de ces conditions est remplie :
- désaccord de fond avec le second avis ;
- sources contradictoires ;
- confiance calculée inférieure à 0,6.

N4 passe toujours en profondeur renforcée. Le rôle public de l'auteur achète la
surveillance et la latence, jamais la profondeur ; la difficulté de l'affirmation
achète la profondeur.

**Juge**, sans outil, ne reçoit que l'affirmation canonique, le cadrage et les
passages. Il renvoie (schéma strict) :

- l'issue de véracité (voir [contexte.md](contexte.md)) ;
- pour chaque assertion de sa justification, l'identifiant du passage et la
  **citation exacte** qui l'appuie ;
- le besoin de contexte (types et éléments) ;
- pour chaque passage utilisé, l'**origine** de l'information (l'entité à qui elle est
  attribuée : un ministère, une agence, une étude), qui sert au contrôle
  d'indépendance ;
- ce que les sources ne disent pas ;
- les points que le relecteur doit vérifier en priorité ;
- une éventuelle demande de recherche supplémentaire (bornée par le niveau).

Il ne renvoie **pas** de score de confiance (calculé en E10).

**Second avis aveugle** : un grand modèle OpenAI (autre fournisseur que le juge,
couvert par l'allocation gratuite) reçoit l'affirmation cadrée et les passages **dans
un autre ordre, sans le verdict**, et rend sa propre issue. Un modèle à qui l'on
demande « la meilleure objection » en trouve toujours une, et il est ancré par le
verdict qu'on lui montre ; l'accord de deux jugements indépendants est un signal bien
plus fort. Le backend compare les deux issues :

| Cas | Définition | Effet |
|---|---|---|
| Accord identique | Même issue | Confiance en hausse |
| Écart de style | Un cran d'écart du même côté (Vrai/Plutôt vrai contre Trompeur/Faux) | Journalisé pour mesurer le décalage entre fournisseurs, sans effet sur la confiance |
| Désaccord de fond | Changement de côté, ou verdict contre abstention | Second appel : **objection typée** ; escalade en profondeur renforcée |

L'objection typée vient d'une liste fermée, avec identifiant de passage obligatoire :
preuve mal lue, preuve insuffisante pour l'issue, source non indépendante, période ou
périmètre erronés, définition différente, cadrage infidèle à l'affirmation. Elle ne
propose pas d'autre issue, seulement une direction fermée (plus sévère, moins sévère,
abstention), et s'affiche au relecteur comme point à vérifier en priorité.

Le second avis voit les passages dans un autre ordre, ce qui couvre déjà le biais de
position. La **seconde passe du juge** n'est donc pas systématique : elle a lieu sur
désaccord et toujours en N4.

### E9 — Rédaction et contrôles déterministes (Anthropic + code)

Un rédacteur (Sonnet 5.5) produit le brouillon : fiche de fact-check et/ou note de
contexte (format dans [contexte.md](contexte.md)). Il reçoit le verdict, les passages
cités et le texte du post (mentions pseudonymisées, délimité comme donnée), pour que la
note réponde à ce que le post dit vraiment. Le verdict est déjà figé : le rédacteur ne
peut pas le changer.

Contrôles déterministes, sans modèle (échec → brouillon rejeté ou rétrogradé) :

| Contrôle | Règle |
|---|---|
| Citations | Chaque citation existe mot pour mot (après normalisation des espaces et guillemets) dans un passage récupéré |
| Indépendance | « Vrai » et « Faux » exigent deux **origines** indépendantes, dont une primaire. Deux passages de même origine (deux journaux qui relaient le même communiqué) comptent une fois, comme cette origine ; le domaine ne sert que de repli. Une source partie prenante ne compte pas comme origine indépendante. À défaut : rétrogradation en « Non vérifiable » avec contexte |
| Chiffres officiels | Une statistique produite sous **indépendance statutaire** (attribut du registre, fondé sur des critères externes) est acceptée seule. Une **auto-déclaration de bilan** n'est primaire que pour l'attribution : sans corroboration, issue « Non vérifiable » et note qui attribue le chiffre, avec celui de l'autre partie s'il existe |
| Dates | Toute source datée après le post est signalée ; une source plus vieille que la péremption du type est signalée |
| Longueurs | Note de contexte ≤ 280 caractères hors liens ; fiche ≤ longueur du profil |
| Lexique | Liste de mots chargés ou de jugements de valeur à éviter (« honteux », « mensonge éhonté »…) : alerte pour le relecteur. Les qualifications juridiques ou historiques établies et sourcées (« génocide », « crime contre l'humanité ») sont autorisées ([objectivite.md](objectivite.md)) |
| Liens | Chaque lien du brouillon pointe vers une source récupérée |

### E10 — Assemblage (code)

- **Confiance calibrée**, calculée et non déclarée par un modèle. Elle prédit la
  probabilité que le relecteur accepte l'issue sans la changer. Elle est écrite dès le
  départ comme une **régression logistique à coefficients posés à la main** (valeurs
  de Fable, non mesurées) :

  | Signal | Coefficient |
  |---|---|
  | Ordonnée | 0 |
  | Source primaire directe | +1,0 |
  | 2 origines indépendantes / 3 ou plus | +0,5 / +0,8 |
  | Sources contradictoires | −1,5 |
  | Second avis du même côté / issue identique / désaccord de fond | +0,8 / +1,1 / −1,5 |
  | Seconde passe concordante / discordante | +0,4 / −1,2 |
  | Statistique avec fiche | +0,5 |
  | Situation en cours / causalité / interprétation juridique | −0,8 / −1,0 / −0,5 |
  | Issue « Trompeur » | −0,4 |
  | Couverture B | −0,3 |

  Confiance = sigmoïde de la somme, **plafonnée à 0,90** avant calibration. Haute à
  partir de 0,80, moyenne à partir de 0,55. À 200 revues, on réestime l'ordonnée et les
  3 ou 4 coefficients les plus forts avec rétrécissement vers ces valeurs ; le modèle
  complet n'est réestimé qu'à partir de 100 refus (200 revues ne donnent qu'environ
  30 refus, trop peu pour une douzaine de coefficients).
- **Contexte du compte** (N3/N4 seulement), ajouté **après** le verdict : récidive sur
  la même affirmation, verdicts antérieurs, corrections publiées. Il enrichit le
  brouillon sans pouvoir influencer le jugement.
- **Revérification** : une lecture de l'API de la plateforme sur le post représentatif
  (toujours en ligne, texte inchangé).
- Création du dossier (cas d'usage `open_dossier`, type `fact_check`) et publication
  de `draft.ready`.

### E11 — Revue et apprentissage (équipe, bot Discord)

Le bot affiche le brouillon (SSE `draft.ready`). Le relecteur valide, corrige ou
rejette ; chaque action passe par un cas d'usage et devient une **donnée** :

- issue finale et diff entre brouillon et version publiée ;
- étiquettes d'apprentissage : faux positif de triage, mauvais regroupement (avec
  scission), mauvaise source, mauvais cadrage ;
- temps de relecture.

Ces données nourrissent la distillation du classifieur E1, le calibrage de la
confiance, les exemples dynamiques (les trois cas corrigés les plus proches, ajoutés
après le préfixe en cache) et le jeu de référence. La publication sur X reste
manuelle (« Marquer comme publié »).

## Intégration dans Tower

### Services

Le fact-check est découpé en services, pour isoler les charges sur des réplicas
hangar dédiés (`TOWER_SERVICES`, `TOWER_QUEUES`). Chaque service n'importe que
`tower.core`.

| Service | Rôle | Files | Réplica type |
|---|---|---|---|
| `ingest` | Réception des contenus (extension, radar, autres plateformes), `content_items`, `observations` | `ingest` | hangar général |
| `factcheck` | E0 à E5, E10, E11 (tout ce qui est rapide ou par lots) | `fc-bulk`, `fc-fast` | hangar général |
| `investigate` | E6 à E9 (enquêtes, appels Anthropic, collecte) | `fc-deep` | hangar dédié, concurrence modérée |
| `atlas` | Synchronisation des sources (miroir LEGI, flux d'actualité, registre de fiabilité, ClaimReview) | `atlas` | hangar dédié, faible priorité |
| `media` (futur) | M0 à M2 : empreintes, OCR, transcription | `media` | hangar dédié |

Les services communiquent par **événements** (abonnements courts qui enfilent des
jobs) et par **jobs** ; aucun import croisé.

### Topics (à ajouter à `docs/events.md` lors de l'implémentation)

| Topic | Émis par | Payload principal |
|---|---|---|
| `content.observed` | ingest | plateforme, ID externe (chaîne), source d'observation |
| `content.ingested` | ingest | ID interne du contenu, niveau initial |
| `claim.extracted` | factcheck (E2) | IDs des affirmations, contenu source |
| `claim_group.created` / `claim_group.merged` / `claim_group.split` | factcheck (E3), cas d'usage de revue | groupes concernés |
| `claim_group.level_changed` | factcheck (E4) | ancien et nouveau niveau, motif |
| `narrative.surge_detected` / `narrative.surge_ended` | factcheck (E4) | récit, vélocité, niveau d'alerte |
| `investigation.requested` | factcheck (E4) | groupe, niveau, version de profil |
| `investigation.completed` | investigate (E8/E9) | groupe, issue, besoin de contexte |
| `draft.ready` | factcheck (E10) | dossier, groupe, confiance |
| `draft.reviewed` | cas d'usage de revue | décision, diff, étiquettes |
| `verdict.published` / `verdict.expired` | cas d'usage, maintenance | groupe, verdict |
| `budget.threshold_reached` | routeur de modèles | périmètre, pourcentage |

Convention du socle : `<agrégat>.<verbe au participe passé>`, IDs en chaînes.

### Jobs

| Type | File | Idempotence (`dedupe_key`) |
|---|---|---|
| `factcheck.normalize` | `fc-bulk` | `norm:<content_id>` |
| `factcheck.embed_batch` | `fc-bulk` | `emb:<hash du lot>` |
| `factcheck.extract_batch` | `fc-bulk` / `fc-fast` | `ext:<hash des IDs du lot>:<prompt_version>` |
| `factcheck.match` | `fc-bulk` | `match:<claim_id>` |
| `factcheck.schedule` | singleton (verrou consultatif) | — |
| `factcheck.light_check` | `fc-bulk` | `light:<group_id>:<profile_version>` |
| `investigate.frame` / `.collect` / `.judge` / `.second_opinion` / `.objection` / `.draft` | `fc-deep` | `<étape>:<group_id>:<investigation_id>` |
| `factcheck.assemble` | `fc-fast` | `asm:<investigation_id>` |
| `factcheck.revalidate` | `fc-bulk` | `reval:<verdict_id>` |
| `llm.batch_submit` / `llm.batch_poll` | `fc-bulk` | `batch:<provider>:<batch_id>` (`RetryLater` tant que le lot n'est pas fini) |
| `atlas.sync_legi`, `atlas.refresh_feeds`, `atlas.sync_claimreview`, `atlas.refresh_registry` | `atlas` | `<source>:<date>` |

Chaque étape de l'enquête est un job distinct : un crash au milieu ne fait pas
repayer les étapes déjà faites, et l'état de l'enquête est lisible en base
(`investigations.stage`).

### Cache des appels de modèles

Tout appel de modèle passe par le routeur ([modeles.md](modeles.md)), qui calcule une
**clé de contenu** : SHA-256 de (rôle, fournisseur, modèle, version du prompt,
paramètres, entrée canonique). Si la clé existe dans `llm_calls` avec un résultat
valide, le résultat est réutilisé sans appel. Effets : handlers idempotents même en
cas de rejeu, réexécution gratuite d'un job tombé après l'appel, rejeu des
évaluations sans coût.

### Nouveaux ports (`core/ports`)

Les ports existants (`LLMClient`, `SearchClient`, `XClient`) sont des ébauches
destinées à être étendues. Contrats prévus :

| Port | Rôle | Adaptateurs prévus |
|---|---|---|
| `LLMClient` (étendu) | `complete(LLMRequest) -> LLMResult` : sortie structurée validée, usage, coût, ID de requête du fournisseur | `OpenAIResponses`, `AnthropicMessages`, doublure de test |
| `BatchLLMClient` | Soumission et relève de lots (API Batch des deux fournisseurs) | idem |
| `Embedder` | `embed(texts, dims) -> vectors`, modèle et version | `OpenAIEmbedder`, doublure ; un modèle local reste possible plus tard |
| `KnowledgeSource` | `lookup(query) -> Sequence[Evidence]`, capacités déclarées (types d'affirmation, pays, langues), coût unitaire, licence | voir [connaissances.md](connaissances.md) |
| `SearchClient` (étendu) | recherche web, domaines autorisés ou exclus, langue, pays | `BraveSearch`, doublure |
| `WebFetcher` | récupération sûre d'une URL | `HttpFetcher` (httpx), doublure |
| `PlatformClient` (généralise `XClient`) | lire un contenu, son contexte (parent, citation), ses métriques, le profil public de l'auteur | `XClient`, `BlueskyClient`, … (voir [plateformes-medias.md](plateformes-medias.md)) |

Les SDK `openai` et `anthropic` sont importés **paresseusement**, dans les adaptateurs
seulement : gate et les réplicas sans service de fact-check ne les chargent jamais.

### Nouveaux codes d'erreur

Domaines prévus : `TWR-FCK-*` (pipeline), `TWR-LLM-*` (fournisseurs de modèles : quota,
refus, sortie invalide, dépassement de budget), `TWR-ATL-*` (sources de connaissance),
`TWR-ING-*` (ingestion). Exemples : sortie de modèle invalide malgré le schéma
(relançable), budget épuisé (`RetryLater` jusqu'à la période suivante), citation
introuvable (brouillon rejeté, stocké pour analyse), source indisponible (repli sur
la source suivante). À définir dans `core/domain/error_codes.py` avec régénération de
`docs/errors.md`.

### RAM

Les étages sont dominés par l'attente réseau : un réplica asyncio traite des dizaines
d'appels simultanés avec très peu de mémoire. Règles :

- aucun modèle local, aucun numpy, pandas ou lxml dans les chemins de production ;
  extraction HTML par une bibliothèque légère (candidate : `selectolax`, à mesurer) ;
- pages traitées en flux, HTML jeté dès l'extraction, plafond de 2 Mo par page ;
- calculs vectoriels et agrégats dans PostgreSQL ;
- concurrence bornée par file (`TOWER_WORKER_CONCURRENCY`) ;
- miroirs de sources (LEGI) analysés en flux (`xml.etree.ElementTree.iterparse`) ;
- cible : un réplica `investigate` sous 200 Mo de RSS, à mesurer au premier smoke test.
