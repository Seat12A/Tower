# 2026-11-23 — Qualité des fact-checks (examen des cases exportées)

## Objectif

Jules a exporté les 85 cases ouvertes sur les comptes suivis (`cases.jsonl.gz`, 48 posts,
du 2026-11-15 au 2026-11-22) : doublons, verdicts faux, mauvaises sources, notes
bancales. Demande : examiner chaque case, trouver les causes et corriger le pipeline.
Consignes ajoutées en cours de session : pour un post qui attribue un programme, une
déclaration ou une accusation à une personne ou à une organisation, chercher d'abord sa
source officielle (avec prudence), puis les articles ; relire toutes les réponses d'un
œil critique.

## Constat (85 cases, 89 enquêtes, 4,82 $)

Relecture de chaque case par Opus et par 4 sous-agents Sonnet en lecture seule (un
quart des cases chacun). Les relectures recoupent l'analyse automatique.

| Problème | Ampleur | Cause dans le code |
|---|---|---|
| « Non vérifiable » partout | 67 sur 89 | Les trois causes suivantes |
| Sources hors sujet citées et décrites dans la note (date de naissance Wikidata, articles du Code de la sécurité intérieure pour une garde à vue, résultats électoraux de Neuilly-Plaisance pour un corps retrouvé, manifestation à Prague pour Sylvain Durif) | ~40 cases | Brave restreint aux domaines officiels de la juridiction (`gouv.fr`…) pour tous les types, élargissement jamais déclenché (condition de budget impossible avec 8 requêtes pour 4 recherches) ; Wikidata (une propriété = un document) et les codes remplissaient le plafond de 24 documents avant le web ; codes juridiques interrogés pour toute « procédure » ; index d'actualités en `OR` sur un seul mot ; aucun seuil de pertinence avant le juge |
| « Vrai » bien sourcé rétrogradé en « Non vérifiable » (mort de Loana selon Paris Match et le procureur, mort de Radvinsky selon Reuters, panne Apple, série Harry Potter sur le site officiel, candidature de Villepin), et même « 1+1=4 » | 18 rétrogradations | Règle « deux origines dont une primaire » : la presse est `unrated`, jamais primaire ; seule une source officielle pouvait débloquer un verdict |
| Doublons | 85 cases pour 48 posts | Une case par affirmation (conception) + découpage excessif (sondage Elabe en 5 cases, programmes en 4 ou 5) ; revalidation : chaque expiration rouvrait une enquête et une case (cases 10, 11, 85, y compris pour une note manuelle de test) ; même rumeur postée deux fois (similarité 0,99, arbitre « même affirmation ») mise dans deux groupes |
| Affirmations non vérifiables extraites | ~12 | « À la Philharmonie, il y a plein d'origines » (propos d'un rappeur traités comme un fait), « on est perdus dans les Keys à faire du kayak », « Mélenchon a réagi » |
| Jugement à la date de vérification | ~10 | GTA 6 « officiellement disponible » jugé faux (articles de la veille), notes « Au 21 novembre à 18 h 32 UTC » pour des posts de mars |
| Notes en jargon d'enquête | ~35 | « les sources fournies », « l'extrait consulté », « la fiche Wikidata consultée » |
| Note en langue `und` | 4 | Langue du post indéterminée reprise telle quelle |

## Réalisé

- **Recherche web** (`core/adapters/knowledge/atlas.py`) : tout le web pour les faits,
  citations, conduites et accusations ; restriction aux domaines officiels seulement pour
  statistique, texte juridique et procédure, avec un élargissement qui ne prive jamais un
  côté de sa recherche ; **site officiel du sujet cherché en premier** (`subject_domains`,
  résolu avant la collecte dans `services/investigate/jobs.py`, parti politique P102
  ajouté dans `wikidata_sites.py`) ; places réservées aux pages web ; codes juridiques
  seulement pour `legal_text`.
- **Index d'actualités** (`news_index.py`, `match_query`) : deux mots communs au moins.
- **Seuil de pertinence** (`domain/factcheck/passages.py`, `select_passages`) : un passage
  sous 15 % du meilleur score BM25 n'est pas donné au juge
  (`TOWER_FC_PASSAGE_MIN_RELATIVE_SCORE`).
- **Règle d'indépendance** (`domain/factcheck/independence.py`) : deux origines
  indépendantes et une source primaire **ou** deux sources établies (statut du registre
  ou de la table d'identité, jamais du modèle).
- **Revalidation à la demande** (`services/factcheck/revalidation.py`) : nouvelle
  enquête seulement si un post a rejoint le groupe depuis le verdict ; jamais pour une
  note manuelle.
- **Regroupement** (`services/factcheck/matching.py`, `usecases/claims.py`) : hors
  statistique, signature compatible + similarité ≥ 0,92 + arbitre « même affirmation »
  → rattachement (`compatible_signature:arbiter`).
- **Langue des notes** (`writer_languages`) : une langue indéterminée n'est plus une
  langue de note.
- **Prompts** : `extract.md`, `extract_batch.md` (1 à 3 affirmations, un sondage = une
  affirmation, propos rapportés = attribution, pas d'expérience personnelle ni de texte
  de test, `procedure` redéfini, programme = `quote`) ; `frame.md` (date du post, document
  primaire d'abord pour une affirmation attribuée) ; `query_writer.md` (programme,
  communiqué, institut de sondage d'abord, média cité, mois et année) ; `judge.md` et
  `second_opinion.md` (date du post, passage hors sujet jamais cité) ; `writer.md` (pas
  d'horodatage de vérification, pas de jargon, jamais de source hors sujet décrite).
- Documentation : `fact-check.md`, `factcheck/sources.md`, `configuration.md`,
  `.env.example`, `decisions.md`.

## Décisions

Toutes reportées dans [decisions.md](../decisions.md#qualité-des-fact-checks-après-lexport-des-cases-2026-11-23).
La principale : la règle « deux origines dont une primaire » devient « deux origines
indépendantes, avec une source primaire ou deux sources établies ». Elle reste fermée,
symétrique et déterministe (le statut vient du registre humain ou de la table d'identité
Wikidata ; deux relais d'une même dépêche restent une seule origine).

## Vérifications

- Tests ajoutés : recherche sur tout le web pour un fait, site officiel d'abord, places
  réservées au web, codes seulement pour `legal_text`, index à deux mots, seuil de
  pertinence, corroboration par deux médias établis (et refus pour un même relais, un
  blog ou Wikipédia), revalidation dormante et note manuelle, rattachement d'une rumeur
  identique, langue `und`.
- `ruff format --check`, `ruff check`, `mypy` strict (425 fichiers) : verts.
- Fichiers de tests touchés (Atlas, index d'actualités, domaine fact-check, enquête,
  appariement, service fact-check, expiration, statuts des sources) : verts.
- Suite complète : lancée, **pas terminée au moment du push** (suite longue en série
  dans ce conteneur) ; à relancer avant fusion.
- `alembic check` : aucune différence (pas de migration).
- RSS du hangar (`TOWER_SERVICES=factcheck,investigate`, doublures, mesure à 8 s, base
  dans un worktree de `main`) : 89,5 / 84,9 Mo avant, 85,1 / 85,0 Mo après : écart nul
  (aucun import ajouté).
- **Rien n'a été appelé pour de vrai** : ni modèle, ni Brave, ni Wikidata. Les nouvelles
  consignes et la nouvelle collecte ne sont pas mesurées sur de vrais posts.

## Points ouverts / suite

- **Relancer un échantillon** des posts de l'export en production (par exemple les
  posts Loana, GIMS, Madoua, Glucksmann, sondage Elabe, GTA 6) et comparer : c'est la
  seule vraie mesure des changements. Les cases existantes n'ont pas été modifiées.
- Une case par affirmation reste la conception : un post à trois affirmations donne
  trois cases. Regrouper les brouillons d'un même post dans une seule case est un
  chantier séparé (bot, revue, publication), à décider avec Jules.
- `sources_after_post` se déclenche sur des dates d'exploration (pages sans date de
  publication) : faux signaux à corriger.
- 4 échecs Brave (`TWR-ATL-003`) et 13 indisponibilités GDELT dans l'export, non
  analysés.
- Case 11 : un éditeur a corrigé le verdict en « Vrai » alors que la note dit « non
  confirmé ». Un contrôle de cohérence verdict/note à la relecture serait utile.
- L'export ne contient ni le texte du post cité ou parent, ni les médias : certaines
  affirmations (« Jean-Michel dénonce… ») perdent leur sujet à l'extraction.

## Suite : jeu de stress et premier passage réel (soir du 2026-11-23)

### Réalisé
- **Jeu de stress** de 50 posts (`src/tower/core/data/factcheck_stress_2026_11.jsonl`,
  `--set builtin:factcheck_stress_2026_11`) écrit à partir de l'actualité réelle vérifiée
  en ligne : posts à plusieurs affirmations mêlées, chiffres déformés, rumeurs, accusations,
  avis, injections, paires ; styles des comptes relais et des utilisateurs (voir
  `factcheck/evaluation.md`). Livré dans le paquet (l'image Docker n'a pas `tests/`).
- **Rapport d'évaluation** : section « Details » par cas (rétrogradation, origines, source
  primaire, second avis, note).
- **Premier passage réel** (Jules, console Railway, 25 cas, 0,75 $) : 11/24 exacts, 75 %
  d'abstention. La lecture des sorties du juge (`llm_calls`) montre que le juge avait le bon
  verdict et les bonnes sources dans la plupart des cas ; la **règle d'indépendance**
  rétrogradait ensuite. Corrigé :
  - une source primaire non partie prenante suffit (Insee pour son chiffre, parquet) ;
  - affirmation `quote` : deux éditeurs établis distincts suffisent (l'origine d'une
    citation est toujours l'orateur).
- **Bug** : un résultat Brave contenant U+0000 faisait échouer l'écriture de
  `source_cache` (cas en erreur) ; caractères NUL retirés avant stockage.
- **Pages bloquées** (403 de Le Figaro, Le Parisien, Légifrance, interieur.gouv.fr ;
  robots.txt de Ouest-France) : le titre et l'extrait de l'API de recherche sont gardés
  (`brave_snippet`) au lieu de perdre l'article.
- **Dates** : l'âge de page de Brave n'est plus pris comme date de publication (fausses
  alertes « source postérieure au post »).
- **Noms sur X** : le contrôle de fidélité accepte le nom de famille seul ou à une faute
  près pour une personne (« Riner », « Rinner ») ; l'extraction recopie les noms tels
  qu'écrits (surnoms compris).
- **Attendus corrigés** : 5 issues attendues du jeu étaient fausses (Palisades : condamnation
  du 20 novembre selon le ministère américain de la Justice ; retrait de Ruffin ;
  prolongation de la COP31 ; peines réelles du procès Zecler). Le juge avait trouvé mieux.
- Consigne de Jules notée dans `CLAUDE.md` : tests proportionnés pour un ajustement.

### Constats du passage réel non encore corrigés
- Les coûts à 0 des modèles `gpt-5.6-*` sont l'allocation gratuite OpenAI (voulu).
- GDELT : délais et 429, sauté sans conséquence.
- Moutouh « condamné » et aveux du RN restaient « Non vérifiable » malgré un juge « Faux » :
  cause probable, passages tirés de sites qui recopient les dépêches AFP, non reconnus
  comme presse ; à confirmer avec la section « Details » au prochain passage.

### Vérifications
- Suite complète : 2 256 réussis, 2 échecs (`test_source_standing_db.py` :
  `test_identity_refresh_errors`, `test_subject_sites_failure_does_not_stop_the_investigation`,
  le compte de `error_reports` vaut 0). **Échecs préexistants** : identiques sur le code du
  début de session (commit `3760138`), non liés à ces changements ; à examiner à part.
  Un vrai défaut a été trouvé par la suite et corrigé : le statut donné par le juge
  (affichage) comptait comme « source établie » ; la règle n'utilise plus que le niveau du
  registre, de la table d'identité ou de l'adaptateur.
- `ruff format --check`, `ruff check`, `mypy` strict (425 fichiers) : verts.
- Rien n'a été relancé en production après ces corrections : à faire après déploiement
  (commande dans `factcheck/evaluation.md`).

### Points ouverts
- Relancer le jeu de stress après déploiement et lire la section « Details ».
- Une case par affirmation : regrouper les brouillons d'un post dans une case reste à
  décider avec Jules.
- Case 11 de l'export : contrôle de cohérence verdict/note à la relecture, à décider.
