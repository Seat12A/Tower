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
4. **Fact-check** : plan d'implémentation détaillé dans
   [factcheck/feuille-de-route.md](factcheck/feuille-de-route.md) (2026-11-10). Phase 0
   (jeu de référence de 300 affirmations, comptes et clés, validations juridiques),
   puis lots : fondations (routeur de modèles OpenAI et Anthropic, ports, budgets),
   ingestion générique multi-plateforme, MVP comptes ciblés avec verdict **et note de
   contexte**, V1 (embeddings par API, groupes, cache, profils, niveaux N0 à N4), V2
   (flux de masse, ordonnanceur, récits et mode vague), V3 (Atlas complet : droit,
   statistiques, actualité ; classifieur distillé ; confiance calibrée), plateformes
   supplémentaires, médias, audit et neutralité.
5. **Modération** : définir le cycle et le service `moderation` (type de dossier déjà
   prévu).
6. **blackbox** : archive publique des verdicts.

## Questions ouvertes (héritées du plan Fable)

Liste à jour et complétée : [factcheck/questions.md](factcheck/questions.md).

Capacité réelle de relecture (fixe le quota de brouillons), application automatique
des verdicts à tout un groupe, volumes réels, budget mensuel (API X incluse ou non),
latence acceptable sur un post viral, identité stable des installations, grille
d'étiquetage politique, images et vidéos dans le périmètre, hébergement du modèle
d'embeddings, conservation des pages sources.
