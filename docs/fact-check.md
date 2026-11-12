# Fact-check : implémentation (lots 3 et 4)

Cette page décrit ce qui est **implémenté** dans Tower pour le fact-check. Le plan
complet (raisons, recherches, lots suivants) est dans [factcheck/](factcheck/README.md) ;
en cas d'écart, cette page décrit le code, et l'écart est noté dans
[decisions.md](decisions.md).

Périmètre du lot 3 : les **comptes suivis** (niveau N3) et les **demandes manuelles** de
l'équipe (niveau N4), de bout en bout jusqu'au brouillon relu dans Discord et au
verdict publié en lecture publique. Le **lot 4** (V1) ajoute les embeddings et le
routage E1, l'appariement E3 et les groupes, la réutilisation des verdicts, les fiches de
référence et les bandes de valeurs, la péremption, les profils de traitement et les
forçages de niveau : voir [Lot 4](#lot-4--v1--embeddings-groupes-réutilisation-profils).
Hors périmètre : flux de masse N1/N2 (extraction en lot, promotion N2), vérification
légère E5, ordonnanceur et vagues (lot 5), règle « partie prenante » complète, médias
(voir [factcheck/feuille-de-route.md](factcheck/feuille-de-route.md)).

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

## Lot 4 — V1 : embeddings, groupes, réutilisation, profils

État : **livré en développement, sans clé d'API** (doublures `TOWER_LLM_FAKE=1`, y compris
pour les embeddings). La qualité du regroupement n'est pas mesurée sur un vrai corpus.

### Flux ajouté

```
content.ingested ──(factcheck, abonnement « embed »)──▶ factcheck.embed_batch (fc-bulk, E1)
                                         │  content_embeddings (N0/N1, score, échantillon)
factcheck.extract (E2) ─▶ E3 : embeddings des affirmations, groupes proches (HNSW),
   signatures, fiche et bande, arbitre ─▶ record_extraction
        ├─ rattachement à un groupe (signature identique) ─▶ verdict.reused ou rien
        ├─ nouveau groupe couvert par une bande validée ─▶ verdict hérité, verdict.reused
        └─ nouveau groupe ─▶ claim_group.created + investigation.requested (lot 3)
factcheck:expire (600 s) ─▶ verdict.expired ─▶ factcheck.revalidate (fc-bulk)
```

### E1 : embeddings et routage

- L'abonnement `embed` sur `content.ingested` (langue traitée) enfile un job
  `factcheck.embed_batch` dédoublonné par minute (`emb:<AAAAMMJJHHMM>`), retardé de
  `TOWER_FC_EMBED_DEBOUNCE_SECONDS` : les contenus d'une même minute partagent un lot.
  Le job prend jusqu'à `TOWER_FC_EMBED_BATCH_SIZE` contenus sans vecteur (ni copie, ni
  plus vieux que la rétention), les vectorise (`text-embedding-3-small`, 512 dimensions,
  requêtes de 256 textes), puis enfile la suite s'il en reste.
- Service d'embeddings (`core/llm/embeddings.py`) : route `embed`, tarif de
  `llm_models`, réservation du budget sur `global` et sur le niveau du contenu (`N3` si
  l'auteur est suivi, sinon `N1`), un `llm_calls` par requête (sans sortie), cache mémoire
  borné par processus. En mode factice, `FakeEmbedder` : vecteurs déterministes par
  hachage des mots et des trigrammes (deux textes proches donnent des vecteurs proches).
- Score « vérifiable » : régression logistique en Python pur
  (`core/domain/factcheck/router.py`, 512 poids, aucun numpy), modèle actif de
  `router_models`. **Sans modèle actif, règle permissive** : `score` nul, niveau `N1`.
  Avec un modèle : sous le seuil N1 du profil, niveau `N0`, et `sampled = true` pour
  `TOWER_FC_E1_SAMPLE_RATE` (2 %) des contenus écartés.
- Au lot 4, E1 **classe et enregistre** seulement : l'extraction des contenus N1 et des
  contenus échantillonnés arrive au lot 5. Le déclenchement N3 du lot 3 reste immédiat.
- `content_embeddings` : partitions hebdomadaires créées (semaine courante et deux
  suivantes) et supprimées après 14 jours par la tâche `factcheck:embedding_partitions`
  (1 h). Aucun index vectoriel.
- Entraînement hors production : `uv run tower factcheck train-router [--days 14]
  [--min-samples 200] [--holdout 0.2] [--per-stratum-cap 2000] [--dry-run]`. Étiquettes :
  positif si E2 a extrait une affirmation vérifiable ; négatif si l'extraction n'a rien
  donné ou si un relecteur a posé l'étiquette `triage_false_positive` ou
  `not_worth_checking`. Échantillonnage stratifié par langue, niveau et étiquette ; le
  modèle est enregistré **inactif** avec ses mesures. `uv run tower factcheck
  activate-router --version N --reason "…"` refuse (`TWR-FCK-036`) un rappel < 90 %
  sur l'échantillon de validation, sauf `--force`.

### E3 : appariement et groupes

Pour chaque affirmation extraite (service `factcheck`, dans le job `factcheck.extract`,
hors transaction) :

1. **Embeddings** du texte d'origine et, hors français, du texte pivot français ; un
   vecteur déjà calculé pour le même texte est réutilisé.
2. **Groupes proches** : les `TOWER_FC_MATCH_CANDIDATES` (5) plus proches par le vecteur
   pivot (`claim_groups.representative_embedding`, HNSW `halfvec_cosine_ops`,
   `hnsw.iterative_scan = relaxed_order`, groupes fusionnés exclus).
3. **Signatures normalisées** comparées de façon déterministe
   (`core/domain/factcheck/signatures.py`) : type, sujet (QID ou texte normalisé),
   intervalle de valeur après échelle, unité, monnaie, période résolue (« actuellement »
   = date du post), territoire, polarité, variante de définition, base de comparaison.
   Le prédicat, texte libre, n'entre pas dans l'identité.
4. **Décision** (`plan_match`) :

   | Situation | Décision |
   |---|---|
   | Signature identique, même langue, similarité des vecteurs d'origine ≥ seuil (0,92) | Rattachement sans arbitre |
   | Signature identique, paire interlingue (toujours) ou zone grise (0,80 à 0,92) | Arbitre `pair_arbiter` (2 appels au plus) ; rattachement seulement sur « même affirmation » |
   | Signature compatible (un champ absent d'un côté), similarité ≥ 0,80 | Un appel d'arbitre ; même favorable : **nouveau groupe**, groupe proche suggéré (`related_group_id`) pour une fusion manuelle |
   | Différence de valeur, polarité, période, territoire, définition, unité… | Nouveau groupe, sans arbitre |
   | Similarité < 0,80 | Nouveau groupe |

   L'arbitre reçoit les deux **textes d'origine** (chacun dans sa langue, datamarqués) et
   la date de chaque post, jamais l'auteur ; raison fermée (`same_claim`,
   `different_value`, `different_period`, `different_territory`, `different_definition`,
   `opposite_polarity`, `different_subject`) ; « dans le doute : différent ». Un échec de
   l'arbitre vaut « différent » (`TWR-FCK-034`).
5. **Fiche de référence** pour une affirmation chiffrée (fiche validée seulement) et
   **bande** de valeurs ; `outdated` détecté sans modèle quand la valeur correspond à la
   fiche précédente.
6. **Fact-checks externes** (Google Fact Check Tools, par le cache de l'Atlas) pour un
   nouveau groupe : 3 liens au plus, dans les `flags` de l'enquête.

Un **groupe** réunit des affirmations qui disent la même chose et se vérifient par les
mêmes preuves : on n'y entre qu'à signature normalisée identique, et chaque membre est
couvert par le verdict du groupe. Les variantes chiffrées de valeurs différentes forment
des groupes distincts, reliés à la même fiche. Le vecteur représentatif est le vecteur
pivot de l'affirmation représentative (médoïde des membres après fusion ou scission).
Chaque création, rattachement, fusion, scission, changement de niveau ou de représentant
est journalisé dans `claim_group_events` (auteur, motif, règle, similarité).

**Fusion** (`merge_groups`) et **scission** (`split_group`), permission
`claim_groups:manage` (éditeur), depuis l'API ou Discord :
- fusion refusée si seule la source porte un verdict actif (`TWR-FCK-023`) ou si les
  deux verdicts diffèrent (`TWR-FCK-022`) ; source en état `merged`
  (`merged_into_id`) ; une fusion s'annule en scindant les affirmations déplacées
  (listées dans l'événement) ;
- scission : nouveau groupe **sans verdict**, nouvelle enquête ; le verdict du groupe
  d'origine n'est jamais propagé.

### Réutilisation des verdicts

| Règle | Quand | Effet |
|---|---|---|
| `identical_signature` | Rattachement à un groupe dont le verdict est actif et non périmé, hors N4 | `verdict.reused`, aucune enquête, aucune place dans le quota |
| `band` | Nouveau groupe chiffré dont la fiche est validée et dont la bande (fiche × bande × côté) a été validée par un relecteur, hors N4 | Nouveau verdict actif (`reuse_rule = band`, `reused_from_verdict_id`, issue de la bande, péremption = min(verdict d'origine, prochaine publication)), groupe `passive`, `verdict.reused`, hors quota et sans relecture |

- **N4 : jamais de réutilisation directe** : une enquête N4 renforcée est toujours
  ouverte (sources rechargées et rejugées).
- Un groupe dont l'enquête est en cours, ou dont le brouillon attend la relecture,
  accueille la variante sans seconde enquête.
- Notes (régimes de [factcheck/contexte.md](factcheck/contexte.md)) : une note
  provisoire n'est jamais héritée ; un verdict hérité par bande ne montre pas une note de
  régime « formulation » d'un autre groupe (issue seule, avec le lien vers le verdict
  d'origine).
- Chaque réutilisation est tracée (règle, verdict d'origine) et réversible par une
  scission.

### Fiches de référence et bandes

- `reference_facts` : indicateur, variante de définition, valeur, unité, période,
  territoire, source primaire, dates de publication, fiche précédente. Créées depuis un
  verdict validé (bouton « créer une fiche », `create_fact_from_verdict`), à la main
  (`reference_facts:write`, relecteur), ou par l'alimentation INSEE ; toujours
  `pending` jusqu'à la validation par un éditeur (`reference_facts:validate`,
  `reference_fact.validated`). La validation calcule la bande des groupes reliés.
- Correspondance affirmation → fiche, sans modèle : sujet (QID ou alias normalisé) ou
  prédicat = suffixe de l'indicateur, même unité, même territoire, même définition, et
  période contenue (ou fiche la plus récente publiée avant le post pour « actuellement »).
- Bandes (Q9) : intervalle tiré de la précision (« 7 % » = [6,5 ; 7,5[), tolérance
  τ = max(plancher, 5 % de la référence), plancher 0,2 point pour un taux ; bandes
  `contains`, `within_1_tau`, `within_3_tau`, `beyond`, avec le côté (`above`,
  `below`). Une bande se valide une fois (`validate_band`, depuis le brouillon, le bot ou
  l'API ; `verdict_band.validated`) et ne s'applique qu'aux **nouvelles** variantes.
- Alimentation INSEE : `TOWER_FC_INSEE_INDICATORS` (liste JSON) ; tâche
  `factcheck:reference_feed` (24 h) qui enfile un job `factcheck.insee_refresh` par
  indicateur ; la dernière observation de la série devient une fiche `pending`
  (idempotence par `external_key`).

### Péremption et revalidation

- À la validation, `expires_at` = validation + durée du type d'affirmation, plafonnée
  par la prochaine publication de la fiche et à 6 h si la note est provisoire. Durées par
  défaut (jours) : statistique 120, fait historique 3 650, citation 3 650, texte
  juridique 180, situation en cours 2, procédure 14, bilan 365, conduite 365,
  accusation 90, prédiction 30, opinion 30, causalité 365, autre 180
  (`TOWER_FC_VERDICT_TTL_DAYS` pour les changer).
- Tâche `factcheck:expire` (10 min) : verdict périmé → `expired`, groupe
  `to_revalidate` (`queued` pour un verdict hérité), notes expirées, `verdict.expired`,
  job `factcheck.revalidate` ; notes provisoires périmées → `expired`.
- `factcheck.revalidate` : pour une statistique, une fiche plus récente validée est
  reliée et la bande recalculée ; une bande validée couvre-t-elle la variante, elle
  hérite ; sinon nouvelle enquête au niveau du groupe.

### Profils de traitement et niveaux

- `processing_profiles` : réglages versionnés (par niveau : seuil E1, réutilisation
  directe, plafond unitaire, budget mensuel ; seuils d'appariement ; quota de
  brouillons ; échantillon E1). Sans version active, profil par défaut tiré de la
  configuration (version 0). Le profil actif est lu avec un cache de 30 s par processus.
- **Bornes de sécurité** (`TWR-FCK-030`) : seuil de rattachement entre 0,85 et 0,999 ;
  zone grise entre 0,5 et le seuil ; quota ≤ 500 ; échantillon entre 0,5 % et 20 % ;
  seuils E1 entre 0 et 1, N3 ≤ N1, N4 = 0 ; N4 sans réutilisation directe ; plafond
  unitaire ≤ 5 $ et ≤ budget mensuel du niveau.
- Activation et retour arrière : `admin` seulement (`profiles:manage`) ; l'activation
  met à jour les plafonds de la période courante des portées `level:N0..N4` et publie
  `processing_profile.activated` (différences, auteur, motif). Lecture :
  `profiles:read` (relecteur, éditeur).
- Ce que le profil commande : seuils E1 et échantillon, seuils et réutilisation d'E3,
  quota de brouillons (`factcheck.assemble`, `factcheck:drain_quota`), plafond d'une
  enquête en profondeur standard et en N4 (la profondeur renforcée hors N4 garde
  `TOWER_FC_CAP_REINFORCED_USD`).
- `level_overrides` : forçage d'un contenu, d'un groupe ou d'un compte, avec durée
  (≤ 720 h) et motif obligatoires (`levels:override`, éditeur). Effet immédiat sur les
  groupes (`claim_group.level_changed`, enquête N4 si forçage en N4) ; un forçage de
  compte vaut pour les contenus suivants (extraction en N3 ou N4 même si le compte n'est
  pas suivi). Le forçage le plus précis l'emporte (contenu > groupe > compte) ; jamais de
  baisse pendant une enquête en cours ; l'expiration ne fait pas redescendre un groupe.
- Budgets : `tower llm seed` crée les portées `level:N0..N4`
  (`TOWER_FC_LEVEL_BUDGETS_USD`) ; les embeddings sont imputés au niveau du contenu,
  l'arbitre au niveau de l'affirmation.

### Réglages du lot 4

Voir [configuration.md](configuration.md#fact-check-v1-lot-4).

### Mesures du lot 4

Voir le résumé de session [sessions/2026-11-12-lot-4-v1.md](sessions/2026-11-12-lot-4-v1.md)
(RSS, index HNSW, évaluation). Index HNSW des groupes, jeu synthétique de 50 000 groupes
à vecteurs aléatoires (cas défavorable) : 65 Mo d'index ; construction en 25 s avec
`maintenance_work_mem = 64MB` (le graphe déborde après 36 870 vecteurs) et en 12 s avec
128 Mo ; requête des 5 plus proches en 1,1 ms en moyenne (`ef_search = 40`). Pour
~135 000 groupes par an, prévoir environ 175 Mo d'index et `maintenance_work_mem`
≥ 256 Mo lors d'une reconstruction.

## Lot 5 — V2 : flux de masse, ordonnanceur, récits et vagues

État : **livré en développement, sans clé d'API** (doublures `TOWER_LLM_FAKE=1`). Les
seuils (promotion, paliers de vague, rattachement aux récits) sont ceux du plan, non
calibrés sur un vrai flux. Décisions : [decisions.md](decisions.md#lot-5).

### Flux ajouté

```
content_embeddings N1 (ou échantillon) sans content_extractions
  └─ factcheck:compose_batches (60 s) ─ filter_for_surges (échantillonnage des vagues)
       └─ factcheck.extract_batch (fc-bulk, E2 en micro-lots, rôle extract_batch)
            └─ E3 + record_extraction(investigate=False) ─▶ groupes N1 « dormants »
claim_group.created ─(abonnement « narratives »)─▶ factcheck.attach_narrative (fc-bulk)
factcheck:schedule (tick 60 s, passe adaptative, singleton)
  1. métriques des groupes actifs   2. promotions (factcheck.promotion_check) et rétrogradations
  3. waves_step : statistiques, paliers, mode vague   4. priorités
  5. enquêtes N2 (budget level:N2, quota, capacité fc-deep) et vérifications légères N1
factcheck:narratives (1 h) : fermeture, médoïdes, scission suggérée, purges
```

Les niveaux N3 et N4 n'attendent jamais l'ordonnanceur (déclenchement immédiat du lot 3)
et prennent la **voie rapide** : priorité 10 pour leur extraction, chaque étape de leur
enquête et leur assemblage, devant les jobs de masse (priorité 0) quand un même worker
sert plusieurs files.
`TOWER_FC_MASS_FLOW=false` coupe le composeur (les contenus N1 ne sont plus extraits).

### E2 en micro-lots (N1)

- Un contenu est **en attente** quand son embedding est `N1` ou `sampled`, sa langue
  traitée, ce n'est pas une copie, son auteur n'est pas un compte suivi actif (N3 reste
  immédiat) et il n'a pas de ligne `content_extractions`. Le composeur prend les 500 plus
  anciens et ignore ceux déjà présents dans un job `factcheck.extract_batch` en attente.
- Composition (pure, `core/domain/factcheck/batching.py`) : les plus anciens d'abord ;
  jamais deux posts de cosinus > `TOWER_FC_BATCH_DISSIMILARITY` ni deux posts d'un même
  récit dans un lot ; au plus `TOWER_FC_BATCH_SIZE` posts (plafond dur 12) et
  `TOWER_FC_BATCH_MAX_TOKENS` ; un post qui contient un marqueur d'injection de prompt
  (anglais et français) part **seul**. Un lot part quand il est plein ou quand son plus
  ancien post a attendu `TOWER_FC_BATCH_MAX_WAIT_S`.
- Job `factcheck.extract_batch` : rôle `extract_batch`, niveau N1, portée `level:N1`
  (`BudgetExhausted` ⇒ relance plus tard). Un post absent de la réponse
  (`TWR-FCK-039`) ou une affirmation infidèle au texte est rejoué seul (rôle `extract`).
  **Deux échecs de fidélité dans un lot, ou un canari contaminé** (le chiffre unique du
  post canari retrouvé dans l'affirmation d'un autre post, `TWR-FCK-040`) : tout le lot
  est rejoué post par post (`replayed_solo`). Les affirmations du canari ne sont jamais
  enregistrées.
- Audit : chaque position a la probabilité `TOWER_FC_SOLO_AUDIT_RATE` d'être rejouée
  seule (`extraction_audits`, `discordant`) ; le résultat du lot est conservé.
- Enregistrement post par post : E3 puis `record_extraction(level='N1',
  investigate=False)` : les nouveaux groupes sont créés **dormants, sans enquête** ;
  rattachement et réutilisation inchangés (lot 4). Toute extraction, solo ou par lot,
  écrit `content_extractions` (`extracted`, `no_claim`).

### E4 : ordonnanceur

- Intervalle adaptatif : `clamp(min, max, 3600 × cible ÷ débit)`, débit = contenus vus
  dans la dernière heure. Une ligne `scheduler_runs` par passe.
- **Promotion N1 → N2** (une condition suffit) : observateurs distincts (pseudonymes
  d'installation) sur 6 h ≥ `TOWER_FC_PROMOTION_OBSERVERS` ; vélocité (observations de
  la dernière heure ÷ heure précédente, au moins 10) ≥ `TOWER_FC_PROMOTION_VELOCITY_FACTOR` ;
  vues > `TOWER_FC_PROMOTION_VIEWS` ; signal léger `likely_false` avec une portée
  supérieure à la médiane des groupes N1 actifs ; groupe d'un récit en `surge` ou plus.
  Avant toute dépense, le job `factcheck.promotion_check` relit les métriques du post
  représentatif par l'API de la plateforme (portée `provider:x`) : une raison non
  métrique, ou des métriques fraîches au-dessus du seuil, confirment. Sans client pour la
  plateforme : promotion avec `unconfirmed`. Échec de lecture : pas de promotion
  (`TWR-FCK-042`, info).
- **Rétrogradation N2 → N1** après `TOWER_FC_DEMOTION_HOURS` sous toutes les conditions,
  jamais pendant une enquête en cours. Chaque changement : `claim_groups.level`,
  `level_changed_at`, événement de groupe `level_changed`, topic
  `claim_group.level_changed` (raison `promotion`, `demotion`, `wave`).
- **Priorité** (pure, sans terme lié à l'auteur) : `portée × risque du thème ×
  vérifiabilité × nouveauté × facteur de vague ÷ coût du niveau`, avec `portée =
  log1p(vues) + 2 log1p(observateurs) + vélocité` ; risque 3 (santé, élections, sécurité,
  catastrophes), 2 (économie, justice, immigration, international), sinon 1 ; nouveauté 1
  ou 0,3 (revalidation) ; facteur de vague 1, 2 (`watch`), 3 (`surge`), 5 (`alert`) ;
  coût = coût moyen des enquêtes du niveau sur 30 jours (plancher 1 centime).
- **Mise en file** par priorité : enquêtes N2 (profondeur standard, `flags.scheduled`)
  tant que le budget `level:N2` couvre le coût moyen, que le quota quotidien de
  brouillons (hors brouillons de vague) n'est pas atteint et que `fc-deep` compte moins
  de `TOWER_FC_DEEP_QUEUE_CAPACITY` jobs ; sinon le groupe est retenu (`held_back_by` :
  `budget`, `quota`, `capacity` ; `TWR-FCK-045` une fois par passe pour la capacité).
  Vérifications légères pour la part `TOWER_FC_LIGHT_CHECK_SHARE` des nouveaux groupes N1
  les plus prioritaires. Les groupes d'un récit en vague sont traités par l'étape des
  vagues. Un groupe dont le verdict actif a plus de 48 h passe `passive`.

### E5 : vérification légère

Job `factcheck.light_check` (`fc-bulk`), **jamais publiée** : elle n'alimente que la
priorité et la promotion. Affirmation chiffrée avec une fiche validée : signal
déterministe par la bande, sans modèle (`contains` → `likely_true`, `within_1_tau` →
`needs_context`, sinon `likely_false`). Sinon, preuves = fact-checks existants et sources
structurées de l'Atlas (**ni encyclopédie, ni recherche web**), ou, pour un groupe d'un
récit en vague, les passages retenus de l'enquête représentative du récit (dossier de
preuves) ; rôle `light_check`. Aucune preuve : `uncertain` sans appel de modèle. Résultat
dans `claim_groups.light_signal`, `light_checked_at`, `light_evidence`. Échec :
`TWR-FCK-041`. Idempotent par groupe et version de profil.

### Récits

- Job `factcheck.attach_narrative` pour chaque nouveau groupe : candidats = récits actifs
  des 10 groupes les plus proches. Rattachement si **tous** : au moins 2 signaux sur 4 dont
  un non sémantique (sémantique : cosinus à un médoïde ≥
  `TOWER_FC_NARRATIVE_SEMANTIC_THRESHOLD` ; cooccurrence : au moins 3 posts de plus de
  3 comptes portent une affirmation des deux ; entité partagée ; média partagé), au moins
  une ancre spécifique (entité rare, média non générique, lien partagé) et un cosinus
  ≥ `TOWER_FC_NARRATIVE_MEDOID_FLOOR` à au moins un médoïde. Sinon le groupe fonde un
  récit. Dans le doute, pas de rattachement. L'identité de l'auteur n'intervient jamais :
  les comptes sont seulement comptés.
- Médoïdes : fondateur (fixe) + les 2 groupes de plus grande portée, réélus au plus une
  fois par jour. Fermeture après `TOWER_FC_NARRATIVE_CLOSE_DAYS` d'inactivité ; un
  rattachement ultérieur à un récit fermé crée un nouveau récit
  (`previous_narrative_id`). Scission suggérée au-delà de
  `TOWER_FC_NARRATIVE_SPLIT_GROUPS` groupes. Fusion et scission manuelles par cas d'usage
  (`claim_groups:manage`, `TWR-FCK-043`, `TWR-FCK-044`).
- Titre : job `factcheck.narrative_title` (rôle `narrative_title`) quand un récit
  atteint `watch` sans titre ; titre français en brouillon, validé ou corrigé par un
  relecteur (`dossiers:review`).

### Vagues

- `waves_step` (passe de l'ordonnanceur) : pour les récits actifs dans les 6 dernières
  heures (500 au plus), statistiques sur 5 min, 1 h et 6 h (`narrative_stats`) : part du
  flux d'observations, auteurs distincts, plateformes, portée, score de Poisson.
- Paliers (fenêtre d'une heure, `TOWER_FC_WAVE_THRESHOLDS`) : `watch` (part ≥ 1 %,
  comptes ≥ 10) ; `surge` (part ≥ 3 %, comptes ≥ 30 et croissance ≥ 2× ; ou `watch` sur
  2 plateformes séparément ; ou un groupe du récit en N3, toujours avec les planchers de
  `watch`) ; `alert` (part ≥ 8 % et comptes ≥ 80 ; ou thème surveillé au palier `watch`).
  Pendant 24 h après un changement de version des filtres de l'extension, la condition de
  croissance ne peut pas être remplie. Montée immédiate ; fin de vague après
  `TOWER_FC_SURGE_END_HOURS` sous `watch` (`narrative.surge_ended` avec le bilan).
- **Mode vague** (entrée en `surge` ou `alert`) : groupes portés à N2 (N4 en alerte pour le
  représentant), groupe représentatif (plus grande portée, puis affirmation chiffrée)
  enquêté une fois, avec **priorité 10 dans `fc-deep`** pour tous les jobs de son enquête
  (voie rapide, pas de file dédiée) ; les autres groupes reçoivent une vérification légère
  sur le dossier de preuves dès que l'enquête a des passages retenus ;
  `narrative.surge_detected`. Les brouillons de vague (`flags.surge`) ont leur quota
  quotidien `TOWER_FC_SURGE_DAILY_QUOTA` ; quota plein : règles du quota quotidien. Un
  verdict validé pendant une vague expire au plus tard
  `TOWER_FC_SURGE_VERDICT_TTL_HOURS` après sa validation.
- **Échantillonnage** : au-delà de `TOWER_FC_SAMPLING_PER_MINUTE` posts par minute proches
  (cosinus ≥ 0,85 à un médoïde) d'un récit en vague, seul un échantillon aléatoire
  stratifié (plateforme, heure) est extrait ; le reste est compté (`sampled_out`). Les
  empreintes de nouveauté des posts écartés (nombres, liens, mentions, médias, entités ;
  code seulement) sont comptées dans `narrative_novelty` : une empreinte vue dans
  `TOWER_FC_NOVELTY_POSTS` posts de `TOWER_FC_NOVELTY_ACCOUNTS` comptes force
  l'extraction de son post le plus engagé, dans la limite de
  `TOWER_FC_NOVELTY_BUDGET_SHARE` des extractions horaires du récit.

### Signaux de coordination

Informatifs seulement (affichés dans le brouillon), jamais utilisés pour un verdict ni
pour la priorité, jamais fondés sur un bord politique supposé : voir l'API des récits
ci-dessous.

<!-- lot5-api -->

### Réglages du lot 5

Voir [configuration.md](configuration.md#fact-check-v2-lot-5).

### Mesures du lot 5

Voir le résumé de session [sessions/2026-11-12-lot-5-v2.md](sessions/2026-11-12-lot-5-v2.md).

## Données

Tables du lot 3 : [database.md](database.md#fact-check-lot-3) ; du lot 4 :
[database.md](database.md#fact-check-v1-lot-4) ; du lot 5 :
[database.md](database.md#fact-check-v2-lot-5). Événements :
[events.md](events.md). Codes : `TWR-FCK-*` et `TWR-ATL-*` dans [errors.md](errors.md).
