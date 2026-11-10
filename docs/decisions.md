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
