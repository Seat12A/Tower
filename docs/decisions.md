# Journal des décisions

Format : date, décision, raison. Les décisions écartées sont conservées pour ne pas
rouvrir les mêmes débats.

## Produit et données

| Date | Décision | Raison |
|---|---|---|
| 2026-11 | Aucune publication automatique sur X : le bot notifie, l'équipe publie et colle le lien | Contrôle éditorial humain, traçabilité |
| 2026-11 | Copie du post X stockée à l'ouverture d'un dossier | Les posts peuvent être modifiés ou supprimés |
| 2026-11 | Deux sources de surveillance : extension « bridée » et API officielle de X sur comptes ciblés | Coût maîtrisé, conformité |
| 2026-11 | Données de l'extension non fiables par défaut ; relecture via l'API X avant ouverture de dossier (une lecture par dossier) | Résistance aux signalements coordonnés |
| 2026-11 | Liste des comptes politiques surveillés équilibrée sur tout le spectre, idéalement publique | Neutralité mesurable |

### Écartées

| Option | Raison du rejet |
|---|---|
| Comptes X automatisés pour lire les posts (scraping) | Interdit par les conditions d'X ; dommages-intérêts forfaitaires (15 000 par million de posts au-delà d'un million en 24 h) |
| Extension qui collecte tout ce que voit l'utilisateur | Responsabilité comme facilitateur, RGPD, règles du Chrome Web Store |

## Architecture (socle, 2026-11-10)

| Décision | Raison |
|---|---|
| Monolithe modulaire, une image, N processus (gate, hangar, radar, intercom) | Une seule base de code, déploiement simple, scalabilité par processus |
| PostgreSQL comme seule infrastructure (données, file, bus, verrous) | Moins de pièces, transactions communes ; Redis seulement sur mesure |
| Outbox transactionnelle + LISTEN/NOTIFY, lecture par `(xid, id)` | Pas d'événement perdu ni fantôme, pas de trou de lecture |
| File de jobs maison (`SKIP LOCKED`, bail, reaper) plutôt que Celery/Arq | Zéro dépendance, transactionnelle avec les données, assez rapide pour nos volumes |
| SQLAlchemy 2 asynchrone (asyncpg) | Cohérent avec FastAPI et discord.py, un processus asyncio par réplica |
| Le bot passe par gate, jamais par la base ; il reçoit les événements en SSE | Un seul point d'autorisation ; SSE plus simple qu'un WebSocket, reprise native |
| Permissions vérifiées dans les cas d'usage, staff = rôles ∩ scopes du service | Défense en profondeur |
| Codes d'erreur stables `TWR-*` et stockage agrégé par empreinte | Diagnostic rapide, table petite, alertes sans spam |
| Logs JSON sur une ligne, stdout uniquement | Format attendu par Railway |
| Statuts en TEXT + CHECK, IDs X en BIGINT exposés en chaînes | Migrations simples ; précision JavaScript |
| Imports paresseux par processus, pools SQL petits, uvloop | Railway facture la RAM |

### Écartées

| Option | Raison du rejet |
|---|---|
| Redis dès le départ | Pièce de plus sans besoin mesuré |
| Enums PostgreSQL | `ALTER TYPE` pénible dans les migrations |
| Plusieurs workers uvicorn par conteneur | RAM multipliée ; on scale en réplicas |
| Accès direct du bot à la base | Contournerait l'autorisation et l'audit |

## Fact-check automatisé (plan Fable du 2026-11-10, résumé)

Voir [roadmap.md](roadmap.md). Principes retenus : on vérifie des **affirmations**,
pas des posts ; cascade E0 à E8 ; signature structurée à sept champs pour réutiliser
un verdict ; fiches de référence (cache de faits) ; collecte hors modèle et juge sans
outil ; verdict aveugle (le juge ne voit ni l'auteur, ni son bord, ni l'engagement) ;
aucun verdict sans sources ; profils Masse, Viral, Ciblé, Critique, versionnés en
base et modifiables à chaud ; budgets avec dégradation. Le goulot est le temps de
relecture humaine.

## Fact-check : plan d'implémentation (2026-11-10, proposé, à valider par Jules)

Détail et justification : [factcheck/](factcheck/README.md). Ces décisions orientent
l'implémentation ; celles qui dépendent d'une réponse de Jules sont signalées.

| Décision | Raison |
|---|---|
| Routage des modèles par **rôle**, table versionnée en base (`llm_routes`) ; le code ne nomme jamais un modèle | Changer de modèle ou de fournisseur sans déploiement ; rejouabilité |
| OpenAI pour les petits rôles (extraction, arbitrage, vérification légère, requêtes) et le contradicteur ; Anthropic pour le cadrage, le jugement, la rédaction et l'audit | Tokens gratuits OpenAI ; raisonnement Anthropic ; juge et contradicteur de fournisseurs différents (erreurs décorrélées, pas d'auto-préférence) |
| Comptage de l'allocation gratuite OpenAI et bascule avant dépassement ; repli payant pour chaque rôle | Une requête qui dépasse l'allocation est facturée en entier ; le programme peut s'arrêter |
| Seules des données publiques, mentions pseudonymisées, vont vers une organisation OpenAI avec partage (**dépend de la validation juridique**) | Les données partagées peuvent servir à l'entraînement d'OpenAI |
| Embeddings par API (`text-embedding-3-small`, 512 dimensions, `halfvec`), pas de modèle local | Moins de 1 $/mois contre 20 à 25 $/mois de RAM Railway pour un modèle local |
| Aucun numpy, pandas, lxml ni modèle local dans les chemins de production ; calculs vectoriels dans PostgreSQL ; classifieur en Python pur | RAM |
| Cinq niveaux de traitement N0 à N4, clé unique de tous les réglages, avec promotion et rétrogradation | Puissance de calcul proportionnée à la portée et au rôle public |
| Le niveau peut dépendre du **rôle public** de l'auteur, jamais de son bord ; le juge ne voit ni le niveau ni l'auteur | Neutralité (règle 10) ; à confirmer par Fable |
| Atlas : sources intégrées interrogées avant la recherche web (fiches, ClaimReview, Wikidata, statistiques, droit, Wikipédia, actualité, puis web) | Coût ; fiabilité (le contexte curé surpasse la recherche web) |
| Wikipédia comme source tertiaire : révision figée, contrôle « revert risk », suivi des références vers les sources primaires | Vandalisme, fiabilité |
| Droit : miroir local LEGI des codes prioritaires français avec dates de vigueur ; adaptateurs à la demande pour l'UE, la Suisse, le Canada et les autres pays | Pas de base mondiale unique ; accès sans recherche web |
| Étage de **cadrage** (E6) avant la collecte | Prendre du recul : définitions, période, sous-entendus, principe de charité |
| Deux axes de sortie : véracité et besoin de contexte (types fermés), note de contexte de 280 caractères au plus | Système de contexte demandé par Jules ; modèle des notes de la communauté |
| Récits (groupes de groupes) et mode vague (z-score robuste, enquête unique, voie rapide, quota réservé, compteurs d'observations, échantillonnage sous charge) | Traiter les rumeurs qui explosent, à coût et RAM bornés |
| Micro-lots de 10 à 20 posts par appel d'extraction | ~× 5 moins de tokens ; tient dans l'allocation gratuite ; à confirmer par Fable |
| Cache de contenu des appels de modèles (`llm_calls`) | Idempotence, rejeu gratuit, évaluations reproductibles |
| Réservation de budget atomique en base (réserver puis régler) | Exacte avec plusieurs réplicas |
| Confiance calculée par le système et calibrée, jamais déclarée par un modèle | Les confiances verbalisées sont gonflées |
| Deux sources indépendantes pour « Vrai » ou « Faux » : passages quasi identiques (dépêche reprise) comptent pour une | Indépendance réelle des sources |
| Couverture mondiale : langue et juridiction distinctes sur chaque affirmation (`text_original`, `text_fr` pivot, `jurisdictions`), registres de sources par pays, niveaux de couverture A/B/C, citations vérifiées sur le texte original | Le fact-check couvre tous les pays et toutes les langues, sans code spécifique à un pays |
| Contenus multi-plateformes : `content_items (platform, external_id TEXT)` + colonne générée `x_post_id BIGINT` | Plateformes futures sans violer la règle des IDs X en BIGINT ; à confirmer par Fable |
| Médias prévus dans le modèle (`media_assets`, étages M0 à M2), livrés plus tard ; la date de première apparition prime sur les détecteurs de deepfakes | Images et vidéos prévues ; les médias détournés sont plus fréquents que les médias fabriqués |
| Conservation des seuls passages cités et de l'empreinte des pages ; respect des opt-out de fouille | Droit d'auteur (L. 122-5-3 CPI), stockage |
| Démarrer avec un seul réplica hangar dédié (`factcheck,investigate,atlas`), séparer par configuration ensuite | Chaque processus Python coûte ~70 Mo de base |

### Écartées

| Option | Raison du rejet |
|---|---|
| Modèle d'embeddings local (bge-m3, e5) sur Railway | Coût RAM permanent très supérieur au coût de l'API |
| Outil de recherche web des fournisseurs (Anthropic, OpenAI) dans le jugement | Plus cher que la collecte par le backend, et donne un outil au modèle qui lit du contenu hostile |
| Une base juridique mondiale unique | N'existe pas : adaptateurs par juridiction |
| Confier la conclusion « campagne coordonnée » ou « deepfake » au système | Indices seulement ; la qualification reste humaine |
| Scraping de l'AFP ou de plateformes sans API | Interdit par les règles du projet et les conditions des services |

### Arbitrages après l'avis de Fable (2026-11-10)

Réponses complètes : [factcheck/reponses-fable.md](factcheck/reponses-fable.md). Ces
arbitrages **remplacent** les propositions correspondantes du tableau ci-dessus.

| Sujet | Décision | Remplace | Raison |
|---|---|---|---|
| Profondeur et niveau N3 | Profondeur d'enquête uniforme (Sonnet, 4 recherches, second avis) ; escalade en profondeur renforcée (Opus, +4 recherches, seconde passe) sur désaccord de fond, sources contradictoires ou confiance < 0,6 ; N4 toujours renforcé. Le rôle public n'achète que la surveillance et la latence | Juge Opus d'office en N3 | Égalité de traitement ; le quota de brouillons rend l'uniformité presque gratuite |
| Mesure de neutralité de la sélection | Rapport brouillons ÷ affirmations vérifiables par bord, à rôle et portée égaux, alerte au-delà de 1,25 sur un trimestre ; 30 groupes non promus enquêtés par mois ; pays à pluralisme faible : titulaires de fonctions + 50 comptes à plus forte audience | — | Mesurer sans que le pipeline connaisse le bord |
| Contradicteur | **Second avis aveugle** (autre fournisseur, sans le verdict, passages dans un autre ordre) ; objection typée seulement en cas de désaccord de fond | Objection systématique au verdict | Un modèle ancré sur un verdict trouve toujours une objection ; l'accord de deux jugements indépendants est un signal plus fort |
| Confiance | Régression logistique à coefficients posés à la main, plafonnée à 0,90 avant calibration ; réestimation partielle à 200 revues, complète à 100 refus | Grille fixe puis régression à 200 revues | 200 revues ne donnent qu'environ 30 refus |
| Seconde passe du juge | Sur désaccord et toujours en N4 | Systématique en N3/N4 | Le second avis couvre déjà le biais de position |
| Lots d'extraction | 10 posts ou 2 500 tokens (plafond 12), N3/N4 en solo, préfixe de 900 tokens, composition par dissemblance, fidélité dans les deux sens, rejeu solo de 2 %, canaris, réglage automatique | Micro-lots de 10 à 20 | Moins de risque de contamination pour presque le même coût |
| Récits | Un récit partage un **dossier de preuves**, pas un verdict ; rattachement à deux signaux dont un non sémantique et une ancre spécifique ; médoïdes ; clôture après 7 jours, scission au-delà de 25 groupes ; variantes jugées à bas coût sur le dossier | Une enquête et un verdict pour tout le récit | Séparer détection (lâche) et application (stricte) |
| Détection de vague | Part du flux observé, écart de type Poisson avec plancher, seuils absolus jusqu'à 1 000 récits clos puis quantiles ; filtres de l'extension versionnés ; veille sur 2 plateformes = vague | z-score robuste par thème | Le z-score dégénère sur des comptages creux |
| Échantillonnage en vague | Passage forcé d'une nouveauté qui prend (3 posts de 3 comptes, ou 90e centile d'engagement), un exemplaire, plafond de 20 % du budget du récit | — | Ne pas rater la variante décisive sans ouvrir une faille de coût |
| Identifiants et partitions | `content_items` non partitionnée, clé interne `BIGINT`, index sur expression pour les IDs X ; `observations` partitionnée | Colonne générée `x_post_id` et partition mensuelle | Une partition impose sa clé dans toute clé unique et étrangère |
| Index vectoriels | HNSW sur les groupes seulement ; vecteurs de posts 14 jours sans index, en partitions hebdomadaires | HNSW sur posts (fenêtre) et affirmations | ~5 Go d'index par an évités |
| Langue pivot | Français tant qu'il est majoritaire (révision au-delà de 50 % de non-français) ; vecteurs du texte d'origine dans une même langue ; signature neutre en langue ; arbitre systématique interlingue | Vecteurs sur `text_fr` pour tout | Ne pas traduire la majorité du volume |
| Réutilisation des verdicts | Directe seulement à signature normalisée identique ; sinon fiche + bande de valeurs (intervalles tirés de la précision, τ = max(plancher, 5 %)), bande validée une fois par un relecteur | Similarité + signature compatible | Fausse réutilisation < 1 % |
| Notes de contexte | 15 types ; trois régimes (objet, formulation, provisoire de 6 h au plus) ; notes rattachées à des objets ; empreinte perceptuelle des images dès l'ingestion | 12 types, notes liées au groupe | Une note sur un média ou une définition vaut partout où l'objet apparaît |
| Indépendance des sources | Par **origine** de l'information ; partie prenante par jointure avec le registre `controlled_by` ; statistique sous indépendance statutaire acceptée seule ; auto-déclaration de bilan → « Non vérifiable » et note d'attribution | SimHash et domaine ; jugement au cadrage | Règle fermée, symétrique, applicable à tous les pays |

Coût estimé après arbitrages : 63 à 91 $ par mois d'IA et de recherche selon le taux
d'escalade (77 $ à 25 %), voir [factcheck/couts.md](factcheck/couts.md).

### Réponses de Jules (2026-11-11)

Détail : [factcheck/questions.md](factcheck/questions.md).

| Décision | Raison |
|---|---|
| Plafond de 50 brouillons par jour, sélection par doute puis portée ; rien n'est publié sans relecture (hypothèse à confirmer) | Capacité de relecture adaptable ; contrôle éditorial humain |
| Partage des données avec OpenAI activé, palier 1 ; minimisation stricte des données envoyées | Tokens gratuits ; avis juridique recommandé |
| Budget « le moins possible » : plafond par défaut de 30 $/mois, escalade Opus suspendue à 80 %, enquêtes non prioritaires suspendues à 100 % | Coût minimal ; le budget borne la dépense, pas le quota |
| Cadence de l'ordonnanceur adaptée au débit (`borne(60 s, 30 min, 5 ÷ débit)`), réveil immédiat pour N3/N4, lots N1 envoyés à 10 posts ou après 6 h | Démarrage à une dizaine de tweets par jour, montée rapide possible |
| Application automatique des verdicts aux variantes de signature identique ou de bande validée | Réponse oui de Jules, dans les limites strictes du plan |
| Liste des comptes suivis tenue librement par l'équipe ; elle ne change que surveillance et latence | Choix éditorial ; publication recommandée |
| Étiquettes politiques limitées aux partis, organisations et affiliations officielles d'élus et de candidats ; jamais déduites pour un compte ordinaire | RGPD article 9 : données rendues publiques par les personnes concernées seulement |
| Registre `controlled_by` amorcé depuis Wikidata et registres officiels, corrigé par l'équipe, publié | Règle de partie prenante applicable sans jugement au cas par cas |
| Brouillons pièges non activés au départ | Petite équipe ; à revoir sur mesure |
| Réponse dans la langue du tweet ; version de référence relue en français ou en anglais ; autres langues en traduction automatique signalée ; langue au choix dans l'extension | Équipe francophone et anglophone ; tout doit être traduisible |
| Plateforme portée par chaque contenu, compte, observation et événement dès le lot 2 | Autres plateformes à venir, système déjà prêt |
| Jeton d'installation transformé en pseudonyme du jour (HMAC avec secret quotidien détruit), aucune IP stockée | Compter les observateurs indépendants sans possibilité technique de profilage ni de traçage |
| Liens des sources toujours conservés, passages cités et empreinte ; lien d'archive publique à étudier | Traçabilité des verdicts ; droit d'auteur |
| Pas de contrat AFP pour l'instant | Budget |

### Objectivité plutôt que faux équilibre (2026-11-11)

| Décision | Raison |
|---|---|
| Le projet vise l'**objectivité** (égalité de traitement, verdict dicté par les preuves), pas une neutralité à mi-chemin ; règle 10 de `CLAUDE.md` reformulée | Demande de Jules : pas de neutralité sur le nazisme et les sujets du même ordre |
| Registre publié `established_facts` à critères fermés (décision de justice définitive, consensus scientifique des organismes de référence, consensus historiographique) ; mêmes critères pour toutes les idéologies | Éviter que l'exception devienne un biais politique |
| Sur un fait établi : issue « Faux » ou « Trompeur », pas d'abstention ; qualifications juridiques et historiques établies autorisées dans les notes | Pas de fausse symétrie |
| L'exception porte sur des faits, jamais sur des personnes ou partis actuels (pas de qualification morale d'un acteur actuel hors décision de justice) | Égalité de traitement |
| Négation, apologie, incitation : signalées aussi à la modération | Relève du droit pénal et de la modération, pas seulement du fact-check |

Détail : [factcheck/objectivite.md](factcheck/objectivite.md).

### Partie 1 : fondations et ingestion (2026-11-11)

Détail : [llm.md](llm.md), [ingestion.md](ingestion.md).

| Décision | Raison |
|---|---|
| Adaptateurs partagés (OpenAI, Anthropic, embeddings, récupération web) dans `core/adapters`, SDK importés dans les constructeurs | Un service n'importe que `tower.core` ; gate et les réplicas sans fact-check ne chargent jamais les SDK (test d'isolation des imports) |
| Repli de modèle fait par le routeur, pas par le repli côté serveur d'Anthropic (`fallbacks`) | Le routeur doit connaître le modèle réellement servi pour le coût, le cache et le journal |
| Un modèle à groupe gratuit n'est jamais appelé en payant : sans allocation suffisante (ou sans clé partagée), il est sauté au profit du repli | Certains sont chers hors allocation (`gpt-5.6-terra`) ; prudence |
| Réservation atomique aussi sur l'allocation gratuite (`llm_free_usage`), puis ajustement au réel | Exact avec plusieurs réplicas, comme le budget |
| Cache = index unique partiel sur la clé parmi les succès ; chaque tentative (refus, sortie invalide, échec) est journalisée et facturée | Le cache ne sert que des sorties valides ; le coût réel inclut les échecs |
| Compteur de budget du mois créé à la première réservation depuis le plafond du mois précédent ; une portée sans aucun compteur n'est pas plafonnée | Pas de tâche planifiée nécessaire ; seul `global` est obligatoire (créé par `tower llm seed`) |
| Famille GPT-6 non marquée gratuite | Éligibilité non vérifiée (à confirmer par Jules dans le tableau de bord) |
| Domaine d'erreurs `TWR-WEB-*` pour la récupération web | Le plan ne prévoyait que `LLM` et `ING` ; les erreurs du fetcher (SSRF, taille, robots) ont leur propre domaine |
| Fetcher : IP résolue et vérifiée à chaque saut, requête envoyée à cette IP (en-tête `Host`, SNI), ports 80 et 443 seulement, `Accept-Encoding: identity` | Protection SSRF sans fenêtre de rebinding DNS ; plafond de taille non contournable par compression |
| Partitions hebdomadaires créées par la migration (semaine précédente à +2) puis par le service `ingest` ; pas de partition par défaut | Une partition par défaut remplie bloquerait la création des partitions suivantes |
| `observed_at` du client accepté seulement dans les 24 dernières heures (5 min d'avance), sinon heure du serveur | Garantit une partition existante et borne la falsification |
| Tâches périodiques singleton déclarées par les services (`Service.periodic`, verrou consultatif) | Partitions et secrets sans processus supplémentaire |
| Liaison des copies écrite par le job sans cas d'usage ni événement | Donnée dérivée et idempotente ; un événement par copie gonflerait l'outbox pendant une vague |
| Audit de la liste de suivi dans `watched_account_events` | Les événements de l'outbox sont purgés après 30 jours |
| Item invalide refusé seul, le reste du lot traité ; quota dépassé : lot entier refusé | Un post mal formé ne doit pas faire perdre le lot ; le quota reste exact |
| Limite par IP en mémoire par réplica, adresse = dernière entrée de `X-Forwarded-For` | Rien n'est persisté ; le quota en base reste la limite exacte |
| Secret du jour créé à la demande par l'ingestion s'il manque, en plus de la maintenance | L'ingestion ne dépend pas du passage de la maintenance |
| Filtres de l'extension publiés par la CLI (`tower ingest publish-filters`), une seule version active | Pas encore d'interface d'administration ; le bot pourra appeler le même cas d'usage |

### Partie 2 : radar et intercom (2026-11-11)

Détail : [radar.md](radar.md), [intercom.md](intercom.md), [llm.md](llm.md#budgets).

| Décision | Raison |
|---|---|
| Radar en **service hangar** `radar`, pas en processus `tower radar` | Un processus coûte environ 70 Mo de RAM, un service hangar quelques Mo ; isolable sur un réplica dédié (`TOWER_SERVICES=radar`) |
| Paramètres de l'API X tirés de la spécification OpenAPI officielle (v2.174) : `post.fields`, `referenced_posts`, `note_post` ; anciens noms lus aussi | Les noms ont changé depuis `tweet.fields` ; pas de clé pour vérifier en réel, les constantes sont isolées dans l'adaptateur |
| Pas d'expansion des posts référencés pendant le polling ; contexte lu à la demande en un appel groupé | X facture chaque ressource renvoyée, `includes` compris |
| `exclude=retweets` par défaut | Le texte d'un repost appartient à son auteur d'origine ; économie de lectures |
| Coût réservé `max_results × prix` puis réglé au réel ; le radar réserve sur `provider:x` seul, pas sur `global` | Le budget des modèles et celui de X sont distincts ; une lecture vide ne coûte rien |
| Radar jamais sans plafond : la planification crée `provider:x` (`TOWER_X_MONTHLY_CAP_USD`, 20 $) s'il manque | Une portée sans compteur n'est pas plafonnée ; inacceptable pour une dépense automatique |
| Plafond quotidien = budget restant ÷ jours restants ÷ (posts attendus par lecture × prix), lectures du jour comptées sur les jobs créés depuis minuit UTC | Pas de table de comptage supplémentaire ; la réservation reste la garde stricte |
| Bail à l'enfilage (`next_poll_at` repoussé d'un intervalle) | Un job en échec n'est pas réenfilé à chaque passage de la planification |
| Lecture d'un compte : règlement, ingestion et planification dans **une** transaction finale | Idempotence : rejouer avant validation relit la même fenêtre (non refacturée sous 24 h), après validation ne trouve rien |
| Colonnes de planification écrites en SQL direct, sans cas d'usage, événement ni incrément de `version` | Donnée opérationnelle dérivée ; incrémenter `version` mettrait en conflit les éditions de l'équipe |
| Ajout par pseudo : `platform_accounts.external_id` nullable, compte « en attente », résolu par le job `radar.resolve_account` via le cas d'usage `resolve_watched_account` | gate ne doit pas appeler X ; l'abonnement reste court |
| Registre commun des budgets `core/budget.py` et table `budget_reservations` (TTL 30 min, libération par la maintenance du socle) | Un processus tué entre réservation et règlement ne bloque plus de budget ; seul un règlement qui supprime réellement la réservation décrémente `reserved` (pas de double décrément) |
| Curseurs SSE des clients rangés dans `event_cursors` (`sse:<client>:<nom>`), sans audit ni événement | Pas de nouvelle table ; un événement de curseur serait renvoyé dans le flux |
| Bot : logique dans des gestionnaires purs, discord.py importé seulement par `intercom/app.py` ; notifications dans la langue `TOWER_DISCORD_LOCALE`, réponses dans la langue Discord de l'utilisateur | Testable sans Discord ; un salon est partagé, une réponse est personnelle |
| Codes `TWR-BOT-*` seulement journalisés | Le bot n'a pas accès à la base (`error_reports`) |

### Lot 3 : MVP du fact-check (2026-11-11)

Détail : [fact-check.md](fact-check.md).

| Décision | Raison |
|---|---|
| Profondeur renforcée par des **rôles distincts** `frame_reinforced` et `judge_reinforced` (routes Opus, repli Sonnet), prompts partagés avec `frame` et `judge` | Le routeur choisit la route par rôle et niveau ; la profondeur ne dépend pas du niveau (escalade en N3), et changer de modèle reste une affaire de base |
| Un groupe par affirmation et une enquête par affirmation vérifiable ; `claim_group_members` historisé | Le lot 4 ajoute le regroupement sans réécriture |
| Schémas de sortie sans contraintes de longueur ni de taille dans le JSON Schema ; bornes vérifiées par pydantic après l'appel | Les sorties structurées d'Anthropic (et le mode strict d'OpenAI) refusent ou ignorent `maxLength`, `maximum`, `maxItems` |
| Passages : découpage d'environ 120 mots, BM25 en Python pur, SimHash ; aucun embedding | Lot 3 sans pgvector ; RAM et coût minimaux |
| Datamarking par le caractère `ˆ` entre les mots des posts et des passages ; citations « démarquées » puis vérifiées sur le texte **original** | Défense contre l'injection (spotlighting) sans fausser le contrôle des citations |
| Escalade : tour 2 (4 recherches de plus, seconde passe du juge renforcé sur tous les passages en ordre inverse) ; une enquête déjà renforcée fait la seconde passe sans nouvelle collecte ; le verdict final est celui de la seconde passe, une divergence devient un point à vérifier | Plan E8 ; l'ordre inverse couvre le biais de position |
| Cadrage « non vérifiable » : pas de brouillon en N3, brouillon « Non vérifiable » en N4 | Ne pas occuper le quota avec des opinions ; une demande manuelle de l'équipe reçoit toujours une réponse |
| Refus d'un modèle : jamais converti en « Vrai » ou « Faux » ; refus du juge → brouillon « Non vérifiable » signalé au relecteur | Règle des refus du plan ; l'équipe tranche |
| La règle d'indépendance ne relève le besoin de contexte qu'en cas de rétrogradation | Un « Vrai » bien sourcé ne doit pas recevoir un besoin de contexte artificiel |
| Revue : `accept` ou `edit` par un `reviewer` = avis favorable (dossier en revue) ; par un `editor` = validation (verdict actif, `verdict.published`) ; `reject` réservé à l'`editor` | Modèle de permissions existant des dossiers (`dossiers:review`, `dossiers:decide`) |
| Origine des versions de la note : `machine` pour tout texte de modèle non relu (y compris la version du rédacteur dans la langue du post), `original` pour une version fr/en validée telle quelle, `human` pour une correction | La lecture publique signale toute traduction ou rédaction non relue (AI Act, article 50) |
| Quota : ouverture immédiate s'il reste une place, sinon attente et tâche `factcheck:drain_quota` (priorité : N4, doute, « Faux »/« Trompeur », portée, ancienneté) | Flux continu : la priorité ne peut s'appliquer qu'aux brouillons en attente |
| Source d'observation `manual` pour les demandes de l'équipe (CHECK élargi en 0005) | Distinguer ces contenus des lectures du radar et de l'extension |
| `dossiers.content_id` (multi-plateforme) ; `x_post_id` conservé pour X | Expand : rien n'est supprimé |
| `verdicts.notified_at` écrit par le bot (marqueur opérationnel, sans audit ni événement) et réconciliation au démarrage | Corrige la limite du notificateur au tout premier démarrage ; idempotent |
| Rejeu de l'extraction avec une consigne ajoutée à l'entrée ; requêtes du tour 2 avec les requêtes précédentes en entrée | Le cache de contenu renverrait sinon la même sortie |
| Pages web récupérées jamais mises en cache ; seules les réponses des API (et des recherches) le sont | Conservation limitée aux passages cités (droit d'auteur, article L. 122-5-3) |
| INSEE BDM interrogé sur `bdm.insee.fr` sans clé ; `TOWER_INSEE_API_KEY` gardée pour un futur portail | Accès ouvert constaté le 2026-11-11 |

Question ouverte (posée à Jules) : le tableau des coefficients de
[factcheck/pipeline.md](factcheck/pipeline.md) donne +0,8 à un second avis « du même
côté », alors que Q3 dit qu'un écart de style est « sans effet sur la confiance ». Le
code applique le tableau ; le coefficient se règle par `TOWER_FC_CONFIDENCE_COEFFICIENTS`.

## Lot 4 (V1 du fact-check, 2026-11-11)

Décisions prises en session ; Jules, absent, a demandé de trancher sans lui.

| Décision | Raison |
|---|---|
| pgvector : template PostgreSQL de Railway avec pgvector (l'image standard ne le fournit pas), bascule par `pg_dump`/`pg_restore` ; la migration `0006` échoue avec `TWR-FCK-035` si l'extension manque ou est < 0.8 | Réponse de Jules à la question posée en début de session ; procédure dans [deployment-railway.md](deployment-railway.md#pgvector) |
| Type `halfvec` maison (`core/db/vector.py`) au lieu du paquet Python `pgvector` | Le paquet tire numpy (RAM) ; vecteurs échangés en texte, calculs dans PostgreSQL |
| Un groupe = des affirmations qui disent la même chose. Rattachement seulement à **signature normalisée identique**, et similarité ≥ 0,92 (même langue) ou avis « même affirmation » de l'arbitre ; arbitre toujours pour une paire interlingue | Tout membre d'un groupe est couvert par le verdict du groupe : rattacher, c'est réutiliser (Q9, règle 1) |
| Signature compatible sans être identique (un champ absent d'un côté) et arbitre favorable : **nouveau groupe**, avec le groupe proche suggéré pour une fusion manuelle | Dans le doute, ne pas réutiliser ; l'équipe fusionne d'un clic |
| Variantes chiffrées de valeurs différentes : groupes distincts reliés à la même fiche ; héritage seulement par une bande validée d'une fiche validée | Q9, règle 2 : jamais de réutilisation par similarité entre variantes chiffrées. Écart avec le scénario du prompt (« un groupe ») : la variante voisine a son propre groupe |
| Le prédicat (texte libre) n'entre pas dans l'identité de la signature ; il sert à résoudre la fiche | Il n'est pas neutre en langue ; la similarité et l'arbitre couvrent le sens |
| Vecteur représentatif des groupes dans l'espace pivot français ; seuil testé sur les vecteurs d'origine pour une paire de même langue | Q8 : vecteur d'origine dans une même langue, pivot pour les autres, un seul index |
| Fusion refusée si seule la source porte un verdict (`TWR-FCK-023`) ou si les deux verdicts diffèrent (`TWR-FCK-022`) ; une fusion s'annule par la scission des affirmations déplacées | Une fusion ne doit jamais étendre un verdict sans décision explicite ; réversibilité par le journal |
| Scission : nouveau groupe sans verdict, nouvelle enquête | Une scission ne propage jamais un verdict |
| Verdict hérité par bande : nouvelle ligne `verdicts` active (`reuse_rule = band`, `reused_from_verdict_id`), hors quota, sans relecture ; note d'origine montrée seulement si elle n'est ni provisoire, ni de régime « formulation » | Traçabilité et réversibilité ; régimes de [factcheck/contexte.md](factcheck/contexte.md) |
| E1 au lot 4 : classe et enregistre (N0/N1, échantillon de 2 % marqué `sampled`), ne déclenche pas d'extraction ; le déclenchement N3 du lot 3 reste immédiat, sans passer par E1 | Le flux de masse (extraction N1, échantillon envoyé à E2) est au lot 5 ; ne pas ajouter de latence au N3 |
| Lots d'embeddings par minute : l'abonnement enfile un job dédoublonné par minute (`emb:<minute>`) retardé de 30 s, qui prend tous les contenus en attente | Lots de 100 à 2 000 textes sans état en mémoire |
| Cache des embeddings : contenus déjà vectorisés sautés, vecteurs d'affirmations réutilisés à texte identique, cache mémoire borné par processus ; un `llm_calls` par requête, sans sortie | Pas de nouvelle table de cache ; aucun texte payé deux fois en pratique |
| Profils : JSON versionné, version 0 = configuration ; bornes vérifiées par le cas d'usage (seuil ≥ 0,85, zone grise < seuil, N4 sans réutilisation et seuil E1 nul, plafond unitaire ≤ budget mensuel…) ; l'activation met à jour les plafonds `level:N*` de la période courante | Modifiable à chaud depuis Discord avec retour arrière |
| Forçage de niveau : le plus précis l'emporte (contenu > groupe > compte) ; jamais de baisse pendant une enquête en cours ; l'expiration ne fait pas redescendre le groupe | Promotion monotone ([factcheck/niveaux.md](factcheck/niveaux.md)) |
| Péremption par type d'affirmation (statistique 120 j, situation en cours 2 j, procédure 14 j…), plafonnée par la prochaine publication de la fiche et à 6 h avec une note provisoire | Valeurs de départ, réglables par `TOWER_FC_VERDICT_TTL_DAYS` |
| Fiches INSEE automatiques créées `pending` : un relecteur valide avant tout usage | Une fiche fausse propagerait des verdicts faux |
| Création de groupe sérialisée par famille de signature (verrou consultatif de transaction) avec une nouvelle recherche pgvector dans la transaction ; seule la règle sans arbitre s'y applique | Le smoke test a montré deux extractions simultanées de la même affirmation créant deux groupes ; l'arbitre (réseau) ne peut pas tourner dans une transaction : une paire interlingue simultanée reste un groupe à part, à fusionner |
| Fusion : les brouillons de la source passent en `superseded` ; la revue d'un brouillon d'un groupe fusionné est refusée (`TWR-FCK-021`) | Un verdict ne doit jamais devenir actif sur un groupe absorbé |
| Copie exacte d'un post (même empreinte) : reliée aux mêmes affirmations par le chemin du lot 3, sans E3 ni `verdict.reused` | La copie porte déjà les affirmations du contenu d'origine, donc son verdict |
| Plafond unitaire du profil : profondeur standard et N4 ; la profondeur renforcée hors N4 garde `TOWER_FC_CAP_REINFORCED_USD` | Le tableau des profondeurs de [factcheck/niveaux.md](factcheck/niveaux.md) distingue standard et renforcée |

## Lot 5 (V2 du fact-check, 2026-11-12)

Décisions de conception arrêtées au début du lot (contrat des sous-agents :
`factcheck/prompts/lot-5-briefs/CONTRACT5.md`) et tranchées en session, Jules absent.

| Décision | Raison |
|---|---|
| API Batch d'Anthropic reportée | L'allocation gratuite couvre le volume de départ ; les micro-lots synchrones suffisent |
| Voie rapide : priorité 10 (`FAST_LANE_PRIORITY`) pour l'extraction N3/N4, toutes les étapes d'une enquête N3/N4 ou marquée `flags.wave`, et leur assemblage (pas de nouvelle file) | Pas de processus ni de file supplémentaires (RAM) ; un worker qui sert plusieurs files les trie par priorité : sans elle, une vague retardait les N3 de 40 s au smoke test |
| Groupes N1 (et N2 issus des lots) créés dormants, sans enquête : l'ordonnanceur décide ; N3 et N4 ne l'attendent jamais | Le coût de masse reste borné ; aucun retard pour les comptes suivis |
| Lot entier rejoué post par post dès 2 échecs de fidélité ou un canari contaminé ; les affirmations du canari ne sont jamais enregistrées | Dans le doute, ne pas faire confiance au lot |
| Un post contenant un marqueur d'injection de prompt est extrait seul | Il ne doit pas pouvoir contaminer les autres posts du lot |
| La promotion N1 → N2 relit toujours les métriques quand un client de plateforme existe ; une raison non métrique confirme dès que la lecture réussit ; lecture en échec ⇒ pas de promotion ; pas de client ⇒ promotion `unconfirmed` | Confirmer avant de dépenser, sans bloquer les plateformes sans API |
| Vérification légère : une fiche validée passe toujours par la bande (déterministe, sans modèle) ; le modèle ne voit jamais la fiche comme preuve | Moins d'appels ; même issue qu'une variante couverte |
| Quota quotidien de brouillons de l'ordonnanceur et de la vidange : brouillons de vague (`flags.surge`) exclus | Ils ont leur propre quota (`TOWER_FC_SURGE_DAILY_QUOTA`) |
| Les règles « N3 » et « 2 plateformes » du palier `surge` exigent aussi les planchers de `watch` ; gel de 24 h après un changement des filtres de l'extension : la croissance ne peut pas être remplie | Un post isolé n'est jamais une vague ; un nouveau filtre fait monter le volume artificiellement |
| Une vague qui monte de `surge` à `alert` republie `narrative.surge_detected` (`tier = alert`) ; la notification du bot est idempotente par récit et palier | L'alerte doit être signalée même si la vague était déjà annoncée |
| Pas de topic `narrative.created` : la fondation d'un récit n'est inscrite qu'au journal `narrative_events` | Un récit par nouveau groupe serait un bruit sans consommateur |
| Le chemin « copie qui hérite des affirmations » n'écrit pas `content_extractions` | Une copie n'est jamais en attente de lot (filtre « pas une copie ») ; écrire écraserait le niveau d'une promotion N4 |

## Périmètre après le lot 5 (Jules, 2026-11-12)

| Décision | Raison |
|---|---|
| Lot 7 (multi-plateforme) reporté : aucune nouvelle plateforme pour l'instant, X seulement | Choix de Jules ; le modèle de données reste multi-plateforme (`platform`) |
| Lot 8 (médias) reporté : ni image ni vidéo pour l'instant | Choix de Jules |
| Lot 9 traité comme un audit (mesure de neutralité), pas comme un lot de fonctionnalités | Choix de Jules ; il suppose des verdicts réels |
