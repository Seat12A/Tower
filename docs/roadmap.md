# Feuille de route

## Fait : socle (2026-11-10)

Ossature, config, logs Railway, base et migrations, domaine des dossiers, outbox et
LISTEN/NOTIFY, file de jobs, worker hangar, services enfichables, auth (tokens,
staff, rôles), gestion d'erreurs (codes, stockage, incidents), gate (dossiers, SSE,
erreurs), Docker, Railway, CI, documentation.

## Prochaines étapes

1. **Ingestion** (`core` + service `ingest`) :
   - tables `observed_posts` (indexée sur l'ID du post X), `observations` (source,
     installation, horodatage), `watched_accounts` (catégorie, priorité, `since_id`),
     `post_snapshots`, `installations` (token anonyme haché, quotas) ;
   - endpoint d'ingestion de l'extension : payload minimal, envoi groupé, plafond
     horaire, rate-limit ;
   - configuration de l'extension pilotée par le serveur (mots-clés, comptes, seuils,
     quotas) ;
   - règles d'ouverture de dossier (plusieurs installations distinctes, mot-clé
     prioritaire) et relecture via l'API X.
2. **radar** : processus de polling de l'API X (priorité → fréquence, `since_id`),
   adaptateur `XClient`, suivi du coût par lecture. Explorer l'accès gratuit
   « utilité publique » d'X.
3. **intercom** : bot Discord (discord.py) client de gate (token de service +
   `X-Acting-Discord-Id`), abonné au flux SSE (`dossier.*`, `incident.*`), action
   « Marquer comme publié ».
4. **Fact-check, MVP** (plan Fable) : comptes ciblés seulement. Filtres E0, triage
   Haiku, collecte hors modèle, juge Sonnet, contrôle des citations, brouillon dans
   Discord, capture des corrections, journal de coûts. Phase 0 avant tout code : jeu
   de référence de 300 affirmations.
5. Fact-check V1 à V4 : embeddings (pgvector, service hangar dédié), regroupement,
   signature structurée, cache de verdicts avec péremption, profils et budgets en
   base, flux de masse, fiches de référence, classifieur distillé, contradicteur,
   audit Fable, tableau de bord de neutralité.
6. **Modération** : définir le cycle et le service `moderation` (type de dossier déjà
   prévu).
7. **blackbox** : archive publique des verdicts.

## Questions ouvertes (héritées du plan Fable)

Capacité réelle de relecture (fixe le quota de brouillons), application automatique
des verdicts à tout un groupe, volumes réels, budget mensuel (API X incluse ou non),
latence acceptable sur un post viral, identité stable des installations, grille
d'étiquetage politique, images et vidéos dans le périmètre, hébergement du modèle
d'embeddings, conservation des pages sources.
