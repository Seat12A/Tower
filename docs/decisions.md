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
