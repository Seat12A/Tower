# 2026-11-14 — Vérification des contenus de l'extension (oEmbed)

## Objectif

Corriger un trou de sécurité trouvé en relisant l'ingestion : le backend croyait sur
parole le texte et l'auteur déclarés par l'extension. Une extension modifiée pouvait
inventer un post attribué à un compte suivi, ce qui lançait une enquête N3 payante, ou
empoisonner un vrai post avant le passage du radar (le « premier écrit gagne » gardait le
faux texte et le vrai post n'était jamais vérifié). Jules a demandé une vérification
**sans dépenser le quota de l'API X**, a validé l'usage d'oEmbed et a demandé des limites
pour « ne pas spammer ».

La session a aussi :
- fait le point sur les branches non fusionnées ;
- reporté les règles permanentes des prompts de Jules dans `CLAUDE.md` ;
- répondu à la question du stockage des tweets pour les statistiques.

## Réalisé

- **Base de travail** : `main` s'arrête au lot 3. Les lots 4 et 5 sont sur
  `claude/friendly-ptolemy-kndvzh` (39 commits non fusionnés). La branche de travail en
  est partie par avance rapide. Les deux commits de `claude/inspiring-wright-ulovhs`
  (prompts d'archive des parties 2 et 3) ne sont pas fusionnés non plus. Les autres
  branches sont contenues dans `main`.
- **`CLAUDE.md`** : nouvelle section « Méthode de travail des sessions ». Elle couvre
  l'autonomie, l'économie d'usage (sous-agents Sonnet **facultatifs**, à la discrétion
  d'Opus, qui implémente lui-même les petites tâches), les exigences transversales, les
  mesures de RAM et les vérifications de fin de lot.
- **Domaine pur** `core/domain/verification.py` :
  - états `unverified`, `platform`, `verified`, `mismatch`, `unavailable` ;
  - lecture du HTML d'oEmbed ;
  - comparaison qui retire les liens et garde les mentions, avec un cas non concluant
    pour les posts longs.
- **Adaptateur** `core/adapters/x_oembed.py` (port `PublicEmbedClient`) : httpx chargé
  à la demande, proxy respecté. Un 404 donne `unavailable` ; 403 et 5xx sont des pannes ;
  un 429 donne une pause.
- **Cas d'usage** `core/usecases/ingestion.py` :
  - un lot d'une source de service marque ses contenus `platform` ;
  - il corrige un contenu `unverified`, `mismatch` ou `unavailable` (texte, auteur,
    langue, empreintes) et publie `content.verified` ;
  - `record_verification` enregistre le résultat de la vérification.
- **Service `ingest`** `services/ingest/verify.py` : tâche singleton `ingest:verify`
  (60 s) et job `ingest.verify` (un seul actif, priorité 10). Le job traite au plus
  `TOWER_OEMBED_PER_MINUTE` contenus, dont `TOWER_OEMBED_CONCURRENCY` à la fois, dans la
  limite de `TOWER_OEMBED_DAILY_MAX` par jour, sur une fenêtre de 24 h. Il fait une pause
  sur 429 et sur panne, et s'arrête à 80 s. Les comptes suivis passent d'abord.
- **Fact-check** :
  - `on_content_ingested` ne déclenche N3 ou un forçage de compte que sur un contenu
    `platform` ou `verified` ; il écoute aussi `content.verified` ;
  - les contenus `mismatch` sont exclus des micro-lots N1.
- **Migration `0008`** (expand) : colonnes `verification` et `checked_at`, CHECK, deux
  index partiels. Rattrapage en `platform` des contenus observés par une source de
  service.
- **Erreurs** : `TWR-ING-012` (divergence), `TWR-ING-013` (oEmbed indisponible),
  `TWR-ING-014` (oEmbed bridé) ; `docs/errors.md` régénéré.
- **Documentation** :
  - `ingestion.md`, nouvelle section « Vérification des contenus » ;
  - `fact-check.md`, `events.md` (`content.verified`), `database.md` ;
  - `configuration.md` et `.env.example` (7 variables `TOWER_OEMBED_*`) ;
  - `decisions.md`, `roadmap.md`.
- **Tests** : `tests/test_content_verification.py` (11 tests : domaine, adaptateur,
  correction par la plateforme, enregistrement, lot et limites, pauses, déduplication,
  N3 qui attend la confirmation) ; `test_ingest_service.py` mis à jour.

## Décisions

Reportées dans [decisions.md](../decisions.md), section « Vérification des contenus de
l'extension ». En bref :
- l'extension n'ouvre jamais seule un fact-check ciblé ;
- la lecture de la plateforme fait foi ;
- oEmbed sert de vérification gratuite et bornée ;
- un 403 n'est jamais pris pour « post absent » ;
- un post long tronqué est non concluant.

Sont écartés :
- le point d'accès non documenté de syndication (assimilable à du scraping) ;
- le consensus entre installations comme preuve (le jeton est généré par l'extension) ;
- la lecture par l'API X de chaque post.

## Vérifications

- **État de départ** (branche `friendly-ptolemy`, PostgreSQL 16 + pgvector 0.8.1 compilé
  dans le conteneur) : **1 787 tests verts**, ce qui confirme les résumés des lots 4 et 5.
- Après le travail :
  - `ruff format --check`, `ruff check` et `mypy` strict (300 fichiers) : OK ;
  - suite complète : **1 797 verts, 1 échec attendu** (`test_errors_doc_is_up_to_date`,
    avant la régénération de `docs/errors.md`) ;
  - après régénération et import paresseux : **suite complète 1 798 verts, 0 échec**
    (4 min 37 s).
- `alembic check` : OK. Aller-retour `--downgrade 0007` puis `migrate` : OK. Rattrapage
  vérifié à la main (radar → `platform`, extension → `unverified`, compteur `manual` →
  `platform`).
- **Appels réels à oEmbed** (`publish.x.com`) :
  - adaptateur seul : post 20 de @jack → texte et auteur corrects ; comparaison
    `verified`, et `mismatch` pour un texte ou un auteur modifié ; post 1 → 404,
    `unavailable` ;
  - **smoke test des vrais processus** (gate en `0.0.0.0` et hangar `ping,ingest`) :
    3 posts envoyés par l'API de l'extension, traités au passage suivant de la tâche :
    20 → `verified` (+ `content.verified`), 21 (texte inventé) → `mismatch`
    (+ `TWR-ING-012`), 1 → `unavailable` ; lot de 3 requêtes en 0,9 s.
- **RSS** (processus lancé directement, mesure à 8 s, deux séries, « avant » = commit
  `200cfb3` dans un worktree) :
  - hangar `ping,ingest` : 70,1 Mo → 70,7 Mo (**+0,6**). La première mesure donnait
    +6,4 Mo : le cas d'usage d'ingestion était importé au chargement du module, il est
    maintenant importé à la demande ;
  - gate : 92,5 à 92,8 Mo dans les deux cas (écart nul).
- **Non vérifié** :
  - limites réelles d'oEmbed sous un volume soutenu. La documentation d'X annonce
    « pas de limite », des sources tierces de 2026 parlent d'un bridage : la pause sur
    429 est testée avec une doublure seulement ;
  - format des posts longs (plus de 280 caractères) dans oEmbed : traité comme non
    concluant par prudence ;
  - format exact du texte envoyé par la vraie extension (liens affichés, emoji en image) :
    la comparaison retire liens et pictogrammes des deux côtés, à confirmer sur de vrais
    envois.

## Points ouverts / suite

- **Fusionner dans `main`** les lots 4 et 5 et ce travail (pull request à demander par
  Jules). Fusionner ou non les deux prompts d'archive de `inspiring-wright`.
- Un contenu corrigé par la plateforme garde son embedding, son lien de copie et ses
  éventuelles affirmations N1 calculés sur l'ancien texte. Rare (il faut qu'un faux texte
  ait précédé la lecture), mais à recalculer si ça s'observe.
- La revérification à l'assemblage reste non bloquante. Elle ne concerne plus que des
  contenus de confiance.
- `promotion_check` (lot 5) lit déjà le post par l'API X avant une promotion : il
  pourrait aussi corriger le contenu (`platform`). Non fait.
- **Statistiques** : tous les contenus reçus sont conservés sans purge, mais
  l'échantillon est biaisé par les filtres de l'extension, les métriques sont écrasées
  (pas d'historique), les observations détaillées sont agrégées après 30 jours. Filtrer
  sur `verification` pour des chiffres fiables. Une table d'historique des métriques
  serait à ajouter si besoin. Point juridique : RGPD et suppression des posts effacés,
  selon les conditions d'X.
- **Environnement cloud** : pgvector n'est pas installé dans l'image ; il a été compilé
  à la main dans cette session (paquet `postgresql-server-dev-16` + sources v0.8.1). Un
  script d'installation de l'environnement éviterait de le refaire à chaque session.
