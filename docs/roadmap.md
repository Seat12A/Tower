# Feuille de route

## Fait : socle (2026-11-10)

Ossature, config, logs Railway, base et migrations, domaine des dossiers, outbox et
LISTEN/NOTIFY, file de jobs, worker hangar, services enfichables, auth (tokens,
staff, rôles), gestion d'erreurs (codes, stockage, incidents), gate (dossiers, SSE,
erreurs), Docker, Railway, CI, documentation.

## Fait : partie 1 du fact-check (2026-11-11)

Lots 1 et 2 de [factcheck/feuille-de-route.md](factcheck/feuille-de-route.md) :
routeur de modèles OpenAI et Anthropic (routes versionnées, cache de contenu,
allocation gratuite, budgets réservés atomiquement), ports et adaptateurs, récupération
web sûre ; ingestion générique multi-plateforme (contenus, comptes, liste de suivi,
observations partitionnées, pseudonymes d'installation du jour, configuration de
l'extension), service `ingest` (copies, partitions, secrets). Voir [llm.md](llm.md) et
[ingestion.md](ingestion.md).

## Fait : lot 3, MVP du fact-check (2026-11-11, en développement)

Comptes suivis (N3) et demandes manuelles (N4) de bout en bout : extraction E2,
cadrage, collecte avec un Atlas minimal (Wikipédia, Wikidata, Fact Check Tools, Brave,
Légifrance, INSEE), jugement, second avis aveugle, objection, escalade, rédaction de la
note et contrôles déterministes, assemblage (confiance, revérification, quota),
brouillons revus dans Discord, traductions, lecture publique du verdict validé, outil
d'évaluation. Voir [fact-check.md](fact-check.md). **Reste** : session de validation et
de réglage avec les clés et le jeu de référence (qualité des verdicts non mesurée).

## Fait : lot 4, V1 du fact-check (2026-11-12, en développement)

Embeddings par API et routage E1 (règle permissive, `train-router`), appariement E3
(pgvector, signatures, arbitre), groupes avec fusion et scission réversibles,
réutilisation stricte des verdicts (signature identique, bandes de valeurs validées,
jamais en N4), fiches de référence (dont INSEE automatique), péremption et
revalidation, profils de traitement versionnés et forçages de niveau, budgets par
niveau, évaluation E3. Voir [fact-check.md](fact-check.md#lot-4--v1--embeddings-groupes-réutilisation-profils).
**Reste** : bascule de la base Railway sur une image avec pgvector, appels réels
(embeddings, arbitre) et mesure du regroupement sur un vrai corpus.

## Fait : lot 5, V2 du fact-check (2026-11-12, en développement)

Flux de masse N1 de l'extension (groupes dormants, copies jamais extraites), micro-lots
E2 (dissimilarité, canari, audit), ordonnanceur E4 singleton (promotion confirmée,
rétrogradation, quota de brouillons), vérification légère E5, voie rapide des N3/N4 et
des vagues, récits et mode vague (paliers watch/surge/alert, quota et TTL de vague,
échantillonnage, nouveautés), signaux de coordination informatifs, commandes
`/narrative`. Voir [fact-check.md](fact-check.md#lot-5--v2--flux-de-masse-ordonnanceur-récits-et-vagues).
**Reste** : coût de masse au-dessus de la cible sous doublures (0,0000155 $ par post
une fois l'allocation gratuite épuisée), rattachement aux récits et échantillonnage non
exercés sans vrais embeddings, appels réels. Mesures :
[sessions/2026-11-12-lot-5-v2.md](sessions/2026-11-12-lot-5-v2.md).

## Fait : vérification des contenus de l'extension (2026-11-14)

Le texte et l'auteur déclarés par l'extension ne déclenchent plus de fact-check ciblé
tant qu'ils ne sont pas confirmés ; une lecture de la plateforme fait foi ; vérification
gratuite et bornée par l'intégration publique d'X (oEmbed). Voir
[ingestion.md](ingestion.md#vérification-des-contenus) et
[sessions/2026-11-14-verification-extension.md](sessions/2026-11-14-verification-extension.md).

## Prochaines étapes

1. **Ingestion (suite)** : règles d'ouverture de dossier (plusieurs installations
   distinctes, mot-clé prioritaire) et relecture via l'API X.
2. ✅ **radar** (partie 2, 2026-11-11) : service hangar `radar`, adaptateur
   `XPlatformClient`, planification adaptative plafonnée par le budget `provider:x`,
   résolution des pseudos ([radar.md](radar.md)). Reste : premier appel réel (clé),
   relecture d'un post par l'API (lot 3), flux Bluesky.
3. ✅ **intercom** (partie 2) : bot Discord client de gate, notifications, commandes
   `/watchlist`, `/dossier`, `/incidents`, `/usage`, reprise du flux par curseur
   stocké ([intercom.md](intercom.md)). Reste : premier lancement réel (token
   Discord) ; la revue des brouillons de fact-check est livrée (lot 3).
4. **Fact-check** : session de validation et de réglage du lot 3 (clés Anthropic,
   OpenAI, Brave, Google, PISTE, INSEE ; au moins 50 affirmations du jeu de référence ;
   `tower factcheck eval`) ; lots 4, 5 et 6 (Atlas complet et outillage de qualité, recalibrage et distillation non activés faute de revues réelles) livrés en développement. Plan d'implémentation détaillé dans
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
