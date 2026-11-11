# Fact-check : implémentation (lot 3, MVP)

Cette page décrit ce qui est **implémenté** dans Tower pour le fact-check. Le plan
complet (raisons, recherches, lots suivants) est dans [factcheck/](factcheck/README.md) ;
en cas d'écart, cette page décrit le code, et l'écart est noté dans
[decisions.md](decisions.md).

Périmètre du lot 3 : les **comptes suivis** (niveau N3) et les **demandes manuelles** de
l'équipe (niveau N4), de bout en bout jusqu'au brouillon relu dans Discord et au
verdict publié en lecture publique. Hors périmètre : regroupement des affirmations,
embeddings, cache de verdicts, flux de masse N1/N2, règle « partie prenante » complète,
médias (voir [factcheck/feuille-de-route.md](factcheck/feuille-de-route.md)).

État : **livré en développement, sans clé d'API**. Les modèles et les sources à clé n'ont
été appelés que par des doublures ; seules Wikipédia et Wikidata ont été appelées pour
de vrai au smoke test. La qualité des verdicts n'est **pas mesurée** : c'est l'objet de la
session de réglage (voir « Évaluation »).

## Flux

```
content.ingested ──(factcheck, filtre N3)──▶ factcheck.extract (E2)
                                              │  claims, groupes, enquêtes
                                              ▼  claim.extracted + investigation.requested
investigation.requested ──(investigate)──▶ investigate.frame (E6)
   ─▶ investigate.collect (E7) ─▶ investigate.judge (E8) ─▶ investigate.second_opinion
   ─▶ [investigate.objection si désaccord de fond] ─▶ investigate.escalate
   ─▶ [tour 2 : collect + seconde passe du juge] ─▶ investigate.draft (E9)
                                              │  verdict brouillon, citations, note
                                              ▼  investigation.completed
investigation.completed ──(factcheck)──▶ factcheck.assemble (E10)
                                              │  confiance, revérification, quota,
                                              ▼  dossier ouvert, draft.ready
draft.ready ──(bot)──▶ message Discord avec boutons ─▶ POST /v1/drafts/{id}/review (E11)
                                              │  draft.reviewed (+ verdict.published)
verdict.published ──(factcheck)──▶ factcheck.translate ─▶ lecture publique
```

Demande manuelle : `/factcheck <url>` ou `POST /v1/factcheck/requests` → job
`factcheck.request` (lecture du post par `PlatformClient.get_item`, budget
`provider:x`, ingestion avec la source d'observation `manual`) → `factcheck.extract` en
N4.

## Services, files et jobs

| Service | File | Jobs | Abonnements | Tâches périodiques |
|---|---|---|---|---|
| `factcheck` | `fc-fast` | `factcheck.extract`, `factcheck.assemble`, `factcheck.request`, `factcheck.translate` | `content.ingested`, `investigation.completed`, `verdict.published` | `factcheck:drain_quota` (60 s), `factcheck:purge_source_cache` (1 h) |
| `investigate` | `fc-deep` | `investigate.frame`, `.collect`, `.judge`, `.second_opinion`, `.objection`, `.escalate`, `.draft` | `investigation.requested` | — |

`investigate` peut tourner sur un réplica dédié (`TOWER_SERVICES=investigate`,
`TOWER_QUEUES=fc-deep`). Les abonnements filtrent et enfilent seulement (aucun appel
réseau).

Idempotence (`dedupe_key`) :

| Job | Clé |
|---|---|
| `factcheck.extract` | `ext:<content_id>:<niveau>` |
| `investigate.<étape>` | `<étape>:<groupe>:<enquête>:<tour>` |
| `factcheck.assemble` | `asm:<enquête>` |
| `factcheck.request` | `req:<plateforme>:<id externe>` |
| `factcheck.translate` | `tr:<verdict>:<version>` |

Chaque étape de l'enquête est un job : l'appel de modèle se fait **hors transaction**
(le cache de contenu `llm_calls` rend un rejeu gratuit), puis une seule transaction
enregistre la sortie, ajoute le coût, fait avancer `investigations.stage` et enfile
l'étape suivante. Un job dont l'étape est déjà passée ne fait rien.

## Déclenchement (N3) et extraction (E2)

- Un `content.ingested` déclenche l'extraction si la langue est dans
  `TOWER_FC_LANGUAGES`, si l'auteur est un compte suivi **actif** et si le contenu n'est
  pas une copie (`copy_of_id`). Une copie exacte (même empreinte) d'un contenu déjà
  extrait est reliée aux mêmes affirmations, sans nouvelle enquête.
- Un post par appel (rôle `extract`). Le modèle reçoit le post datamarqué, mentions
  pseudonymisées (`@user`), et le texte du post parent ou cité tronqué à 280 caractères
  s'il est déjà en base ; jamais l'auteur.
- Contrôle de fidélité dans les deux sens (`core/domain/factcheck/fidelity.py`) :
  chiffre ou entité absent du post → affirmation rejetée ; post à chiffres sans
  affirmation chiffrée → rejeu. Un seul rejeu, avec une consigne ajoutée à l'entrée (donc
  hors cache) ; seules les affirmations fidèles sont gardées.
- Cas d'usage `record_extraction` : `claims`, `content_claims`, un `claim_groups` par
  affirmation (le lot 4 ajoutera le regroupement sans réécriture), `claim_group_members`,
  une `investigations` par affirmation vérifiable, et les événements `claim.extracted` et
  `investigation.requested` dans la même transaction.
- Une demande N4 sur un contenu déjà extrait promeut ses groupes en N4 et ouvre une
  enquête N4 (profondeur renforcée) s'il n'en a pas.

## Enquête (E6 à E9)

| Étape | Rôle | Ce qui est fait |
|---|---|---|
| Cadrage | `frame` (Sonnet) ou `frame_reinforced` (Opus) | Sens littéral, sous-entendus, lecture charitable, définitions, période résolue (date du jour et du post fournies), conditions de vérité et de fausseté, pièges, sous-questions, décision « vérifiable ». Non vérifiable : en N3, l'enquête s'arrête sans brouillon ; en N4, un brouillon « Non vérifiable » est produit pour l'équipe |
| Collecte | `query_writer` puis Atlas | Requêtes symétriques « confirmerait » et « infirmerait » (une série vide est signalée), Atlas (voir plus bas), découpage en passages d'environ 120 mots, classement BM25 en Python, déduplication SimHash, sélection (30 passages en standard, 60 en renforcé, 4 par source au plus). Sans aucun passage, le juge n'est pas appelé : issue « Non vérifiable » |
| Jugement | `judge` ou `judge_reinforced` | Preuves seulement, citation exacte et identifiant de passage par assertion, origine de chaque passage, ce que les sources ne disent pas, points à vérifier, recherche supplémentaire ; pas de score de confiance |
| Second avis | `second_opinion` (OpenAI) | Mêmes entrées, passages en ordre inverse, sans le verdict. Comparaison : identique, écart de style, désaccord de fond |
| Objection | `objection` (OpenAI) | Seulement sur désaccord de fond : type fermé, passage, direction fermée |
| Escalade | code | Voir « Profondeur et escalade » |
| Rédaction | `writer`, `critic`, `translate` | Note dans la langue du post, en français (et en anglais si le post est en anglais), fiche courte ; contrôles déterministes ; critique de pontage (autre fournisseur) ; traduction française des passages cités qui ne sont ni en français ni en anglais |

Les modèles ne reçoivent jamais l'auteur, son bord, l'engagement ni le niveau
(`core/factcheck/inputs.py`, vérifié par `tests/test_factcheck_input_privacy.py`). Le
texte non fiable (post, passages) est datamarqué (`ˆ` entre les mots,
`core/domain/factcheck/datamark.py`) et placé dans le bloc `<input>` du routeur ; aucun
outil n'est donné aux modèles ; chaque sortie est validée par un schéma strict
(`core/factcheck/schemas.py`).

### Profondeur et escalade

- Profondeur initiale : standard (N3), renforcée (N4).
- Après le second avis, une confiance **provisoire** est calculée. Une enquête standard
  passe en profondeur renforcée sur désaccord de fond, sources contradictoires ou
  confiance < `TOWER_FC_ESCALATION_THRESHOLD` (0,6), si le coût déjà engagé est sous le
  plafond : tour 2 avec 4 recherches de plus et seconde passe du juge renforcé sur tous
  les passages en ordre inverse.
- Une enquête renforcée (N4, ou désaccord de fond) fait une seconde passe du juge sans
  nouvelle collecte. Le verdict final est celui de la seconde passe ; une divergence
  entre les deux passes devient un point à vérifier.
- Plafonds par enquête : `TOWER_FC_CAP_STANDARD_USD` (0,10 $),
  `TOWER_FC_CAP_REINFORCED_USD` (0,40 $), `TOWER_FC_CAP_N4_USD` (0,60 $). Au-delà, pas
  d'escalade (`flags.cap_reached`, `TWR-FCK-004`).

### Contrôles déterministes (E9)

| Contrôle | Effet |
|---|---|
| Citation mot pour mot dans le passage **original** (normalisation des espaces, guillemets, tirets, points de suspension ; jamais contre une traduction) | Citation invalide retirée de la justification, gardée en base (`citations.valid = false`), `TWR-FCK-014` |
| Indépendance : « Vrai » ou « Faux » exigent deux origines indépendantes dont une primaire (même origine = une seule ; domaine en repli ; passages quasi identiques = une seule source ; sources `avoid` exclues) | Rétrogradation en « Non vérifiable », contexte au moins « utile » s'il existe des éléments partiels |
| Longueur de la note (≤ 280 caractères hors liens) | Signalée au relecteur |
| Lexique chargé (`core/factcheck/loaded_terms.json`, par langue ; qualifications juridiques et historiques établies autorisées) | Signalé au relecteur |
| Liens : chaque lien vient d'une source récupérée | Lien inconnu retiré et signalé |
| Dates : source postérieure au post, ou trop ancienne pour une situation en cours | Signalée |

### Conservation

À la fin de l'enquête, les passages cités sont marqués `kept` ; le texte des autres
passages est effacé (`text = NULL`). Les lignes `sources` (liens, titre, éditeur, dates,
empreinte, licence) restent toujours.

## Atlas minimal

Adaptateurs dans `core/adapters/knowledge/` (httpx importé paresseusement), derrière les
ports `KnowledgeSource` et `SearchClient`, chaque appel précédé d'une lecture de
`source_cache`. Ordre : fact-checks existants (Google Fact Check Tools), sources
structurées (Wikidata ; INSEE BDM pour une statistique française ; Légifrance via PISTE
pour un texte juridique français), Wikipédia (révision figée, contrôle du risque
d'annulation pour une révision de moins de 48 h, suivi des références vers les sources
primaires), puis recherche web (Brave : domaines primaires de la juridiction d'abord,
puis élargie ; 4 recherches en standard, 4 de plus en renforcé). Une source indisponible
est capturée (`TWR-ATL-*`) et sautée. Ce qui a été vérifié dans la documentation
officielle de chaque API est consigné dans la docstring de chaque adaptateur.

Registre minimal `source_registry` (domaine → niveau, type, pays) amorcé par
`uv run tower factcheck seed-registry` avec une courte liste d'institutions primaires et
d'agences ; tout domaine absent est tertiaire. `controlled_by` et l'indépendance
statutaire arrivent au lot 6.

## Assemblage (E10)

1. **Confiance** : régression logistique à coefficients posés à la main
   (`core/domain/factcheck/confidence.py`, valeurs de [factcheck/pipeline.md](factcheck/pipeline.md)),
   sigmoïde plafonnée à 0,90 ; bandes haute (≥ 0,80), moyenne (≥ 0,55), basse.
   Coefficients modifiables par `TOWER_FC_CONFIDENCE_COEFFICIENTS` (JSON), seuils par
   `TOWER_FC_CONFIDENCE_*`. Signal non disponible au lot 3 : « statistique avec fiche »
   (lot 4), « couverture B » (profils pays).
2. **Revérification** du post représentatif (`get_item`, coût imputé à `provider:x`) :
   supprimé ou modifié → signalé dans le brouillon ; jamais bloquant.
3. **Quota** : `TOWER_FC_DRAFT_DAILY_QUOTA` (50) brouillons par jour UTC, sous verrou
   consultatif. Au-delà, le brouillon attend (`drafted_at` vide) ; la tâche
   `factcheck:drain_quota` ouvre les brouillons en attente dès qu'une place se libère,
   dans l'ordre : N4, cas de doute, issues « Faux » et « Trompeur », portée, ancienneté.
4. **Ouverture** : dossier `fact_check` (avec `content_id`), `dossier_claim_groups`,
   `verdicts.drafted_at`, événement `draft.ready`.

## Revue (E11) et publication

Endpoint `POST /v1/drafts/{id}/review` (voir [api.md](api.md)), boutons du bot (voir
[intercom.md](intercom.md)) :

| Décision | Relecteur (`reviewer`) | Éditeur (`editor`) |
|---|---|---|
| `accept` | Avis favorable enregistré, dossier en revue (`endorsed`) | Verdict actif, dossier `validated`, `verdict.published` |
| `edit` (issue et/ou texte de la note) | Correction proposée enregistrée (`endorsed`) | Verdict actif avec les corrections, `verdict.published` |
| `reject` (motif obligatoire) | Refusé (permission) | Verdict rejeté, dossier `rejected` |

Chaque décision est une ligne `reviews` (issue finale, diff, étiquettes
d'apprentissage, temps de relecture). « Marquer comme publié » sur X reste manuel
(`POST /v1/dossiers/{id}/publish`).

## Langues

- Affirmations, passages et notes portent leur langue.
- Le rédacteur écrit la note dans la langue du post, en français (référence) et en
  anglais si le post est en anglais.
- Les relecteurs voient le français, ou l'anglais selon leur langue Discord ; les
  passages ni français ni anglais reçoivent une traduction française (rôle `translate`).
  Les citations restent vérifiées sur l'original.
- Origine des versions (`context_note_translations.origin`) : `machine` pour tout texte
  de modèle non relu (y compris la version du rédacteur dans la langue du post),
  `original` pour une version du rédacteur validée telle quelle par un relecteur qui la
  lit (fr, en), `human` pour un texte corrigé par l'équipe.
- À la validation, `factcheck.translate` produit les versions fr, en et langue du post
  manquantes (ou périmées après correction), marquées `machine`. La lecture publique
  signale une traduction automatique (`machine_translated`).

## Budgets

Chaque appel réserve sur `global` et sur `level:N3` ou `level:N4` (portées sans compteur
non plafonnées, voir [llm.md](llm.md)). Budget épuisé : `RetryLater` jusqu'à la période
suivante ; au dernier essai, le brouillon part avec ce qui existe
(`flags.budget_incomplete`). Les recherches Brave ont un coût unitaire compté dans le coût
de l'enquête.

## Mode fournisseur factice

`TOWER_LLM_FAKE=1` remplace tous les fournisseurs par `FakeLLMProvider`
(`core/adapters/fake_llm.py`) : sorties valides et déterministes par rôle, citations
copiées des passages. Sert au smoke test et à la démonstration de l'évaluation sans clé.
Refusé quand `TOWER_ENV=prod` (`TWR-FCK-016`, au chargement de la configuration).

## Évaluation

`uv run tower factcheck eval --set <fichier.jsonl> [--limit N] [--report <fichier.md>]`
exécute E6 à E10 sur chaque cas du jeu (sans écrire d'enquête en base : seuls le cache
`llm_calls`, les budgets et `source_cache` sont utilisés, donc un rejeu ne repaie rien),
puis calcule exactitude, taux de faux confiants, abstention, validité des citations, taux
d'escalade, coût moyen et latence. Format du jeu et métriques :
[factcheck/evaluation.md](factcheck/evaluation.md#format-du-jeu-et-commande). Cinq cas
d'exemple, qui **ne sont pas** le jeu de référence : `tests/fixtures/factcheck_eval_examples.jsonl`.

## Réglages

| Variable | Défaut | Rôle |
|---|---|---|
| `TOWER_FC_LANGUAGES` | `fr,en` | Langues traitées en N3 |
| `TOWER_FC_REFERENCE_LANGS` | `fr` | Langues de référence toujours rédigées |
| `TOWER_FC_DRAFT_DAILY_QUOTA` | `50` | Brouillons envoyés en relecture par jour |
| `TOWER_FC_CAP_STANDARD_USD` / `_REINFORCED_USD` / `_N4_USD` | `0.10` / `0.40` / `0.60` | Plafonds par enquête |
| `TOWER_FC_SEARCHES_STANDARD` / `_REINFORCED_EXTRA` | `4` / `4` | Recherches web |
| `TOWER_FC_PASSAGES_STANDARD` / `_REINFORCED` | `30` / `60` | Passages donnés au juge |
| `TOWER_FC_PASSAGE_WORDS` | `120` | Taille des passages |
| `TOWER_FC_CONFIDENCE_COEFFICIENTS` | vide | Surcharge JSON des coefficients |
| `TOWER_FC_CONFIDENCE_CAP` / `_HIGH` / `_MEDIUM` | `0.90` / `0.80` / `0.55` | Plafond et bandes |
| `TOWER_FC_ESCALATION_THRESHOLD` | `0.60` | Seuil d'escalade |
| `TOWER_FC_LOADED_TERMS_FILE` | vide | Lexique chargé (défaut : fichier livré) |
| `TOWER_LLM_FAKE` | `false` | Fournisseur factice (refusé en prod) |

Clés des sources et réglages de l'Atlas : [configuration.md](configuration.md).

## Données

Tables du lot 3 : [database.md](database.md#fact-check-lot-3). Événements :
[events.md](events.md). Codes : `TWR-FCK-*` et `TWR-ATL-*` dans [errors.md](errors.md).
