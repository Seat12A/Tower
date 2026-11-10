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
