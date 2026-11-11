# Prompt d'implémentation — Partie 1 : fondations et ingestion

Prompt à donner tel quel à une session Claude Code (Opus 5.5) qui implémente la
première partie du plan de fact-check. Rédigé le 2026-11-11.

---

Tu es l'agent principal (Claude Opus 5.5) chargé d'implémenter la **partie 1** du
plan de fact-check de 12A Tower : les **fondations communes** (lot 1) et
l'**ingestion générique multi-plateforme** (lot 2). Tu orchestres des sous-agents
**Claude Sonnet 5.5** et tu restes responsable de la qualité, de l'intégration et des
vérifications finales.

## 0. Lire avant toute action (dans cet ordre)

1. `CLAUDE.md` : règles non négociables (langue, architecture, RAM, erreurs, logs,
   définition de « terminé »). Elles priment sur ce prompt en cas de conflit.
2. `docs/sessions/` : les deux derniers résumés (`2026-11-10-*`, `2026-11-11-*`).
3. `docs/factcheck/README.md`, puis `feuille-de-route.md` (lots 1 et 2), `pipeline.md`
   (sections « Intégration dans Tower », E0), `modeles.md`, `donnees.md`,
   `plateformes-medias.md`, `international.md`, `couts.md`, `objectivite.md`.
4. `docs/decisions.md` (sections fact-check, arbitrages de Fable, réponses de Jules) :
   ne rouvre pas une option écartée.
5. Le code du socle : `src/tower/core/` (ports, events, jobs, services, errors,
   auth, usecases), `src/tower/gate/`, `src/tower/hangar/`, `tests/`, et la doc
   `docs/{architecture,database,events,jobs,services,auth,api,configuration,error-handling,development}.md`.

Ne refais pas les recherches déjà documentées : le plan est arrêté. Si un point te
paraît faux ou contradictoire, note-le dans le résumé de session et pose la question,
au lieu de le changer silencieusement.

## 1. Périmètre

### Inclus

**Lot 1 — Fondations**
- Ports étendus dans `core/ports` : `LLMClient` (`LLMRequest`, `LLMResult`),
  `BatchLLMClient` (contrat seulement), `Embedder`, `KnowledgeSource` (+ `Evidence`),
  `SearchClient` étendu, `WebFetcher`, `PlatformClient` (généralise `XClient` ; garder
  `XClient` compatible, règle expand/contract appliquée au code).
- Adaptateurs : OpenAI (API Responses, sortie structurée stricte), Anthropic
  (Messages, `output_config.format`), embeddings OpenAI, `WebFetcher` httpx sûr.
- Routeur de modèles (`core/llm/`) : routes par rôle versionnées en base, cache de
  contenu, comptage de l'allocation gratuite OpenAI, réservation de budget atomique,
  validation de schéma, repli, journal des appels, événements de seuil.
- Registre des prompts (fichiers versionnés par empreinte).
- Tables `llm_models`, `llm_routes`, `llm_calls`, `llm_free_usage`,
  `budget_counters` ; commande `tower llm seed` (idempotente) et `tower llm usage`.

**Lot 2 — Ingestion générique**
- Tables `entities`, `platform_accounts`, `watched_accounts`, `content_items`,
  `content_snapshots`, `media_assets` (+ `content_media`, sans calcul d'empreinte
  pour l'instant), `observations` (partitionnée par semaine), `observation_counters`,
  `installation_daily`, `installation_secrets`, `extension_filter_versions`.
- Domaine pur : plateformes et validation des identifiants, normalisation du texte,
  comptage des mots utiles, détection de langue sans modèle, empreinte exacte,
  SimHash 64 bits et bandes, distance de Hamming, pseudonyme d'installation du jour.
- Cas d'usage d'ingestion, de liste de suivi, endpoint de l'extension, configuration
  de l'extension pilotée par le serveur, service `ingest` (liaison des copies,
  maintenance des partitions et des secrets).

### Exclu (parties suivantes, ne pas commencer)

Radar X et tout appel réel à l'API X ; empreinte perceptuelle des médias (le schéma
la prévoit, le job viendra plus tard) ; étages d'extraction, d'appariement,
d'enquête et de jugement (E1 et au-delà) ; embeddings stockés et pgvector ; API Batch
(implémentation) ; récits et vagues ; Atlas ; bot Discord ; étiquettes politiques ;
cadence adaptative de l'ordonnanceur. Aucun appel réseau réel dans les tests ; aucune
clé d'API n'est disponible, donc tout passe par des doublures.

## 2. Exigences transversales (à vérifier à chaque étape)

- **RAM** (Railway facture environ 10 $ par Go et par mois) :
  - imports paresseux : `openai`, `anthropic`, `httpx` et l'extraction HTML ne sont
    importés que dans les fonctions ou constructeurs des adaptateurs, jamais au niveau
    module dans `core/` ;
  - ajoute un test qui vérifie que `import tower.gate.app` et le démarrage d'un
    hangar avec `TOWER_SERVICES=ping` ne chargent ni `openai` ni `anthropic`
    (contrôle de `sys.modules`) ;
  - aucun numpy, pandas, lxml ni modèle local ;
  - tout cache en mémoire est borné ; aucun état partagé entre requêtes hors base ;
  - mesure le RSS au repos de `tower gate` et `tower hangar` (`TOWER_SERVICES=ping`
    puis `ping,ingest`) avant et après ton travail. Références : gate ≈ 80 Mo, hangar
    ≈ 67 Mo. Cible : au plus +10 Mo pour gate, au plus +15 Mo pour hangar avec
    `ingest`. Reporte les chiffres réels.
- **Scalabilité** : processus sans état ; compteurs et quotas en base ; endpoints par
  lots ; index pensés pour les requêtes réelles ; **pas d'événement par
  observation** (une vague produirait des millions de lignes d'outbox) : un événement
  par nouveau contenu seulement.
- **Langues** : chaque contenu stocke sa langue (`lang`, code BCP 47 ou `und`) et
  l'indice de la plateforme quand il existe ; le texte d'origine est conservé tel
  quel ; la liste des langues traitées est une configuration
  (`TOWER_FC_LANGUAGES`, défaut `fr,en`). Les messages de l'API, des logs et des
  erreurs restent en anglais.
- **Multi-plateforme** : la plateforme est portée par chaque contenu, compte,
  observation et événement ; X est la seule plateforme active, mais l'énumération
  prévoit `x`, `bluesky`, `threads`, `mastodon`, `youtube`, `tiktok`, `telegram`,
  `web`.
- **Économie** : budget par défaut de 30 $ par mois (portée `global`), seuils à 50,
  80 et 100 % ; allocation gratuite OpenAI du palier 1 (2,5 M de tokens par jour pour
  le groupe « petits modèles », 250 000 pour les « grands ») ; les modèles GPT-6 ne
  sont **pas** marqués gratuits tant que Jules ne l'a pas vérifié (prudence).
- **Vie privée** : l'extension n'envoie rien sur l'utilisateur ; le jeton
  d'installation n'est jamais stocké, seul un pseudonyme du jour l'est (HMAC avec un
  secret quotidien détruit) ; aucune adresse IP stockée.
- **Socle** : cas d'usage pour toute écriture métier (permission, audit, événement
  dans la même transaction) ; services n'important que `tower.core` ; handlers de
  jobs idempotents ; statuts en `TEXT` + `CHECK` ; IDs X en `BIGINT` côté base pour
  les requêtes numériques, en chaînes dans l'API ; migrations expand/contract ;
  codes d'erreur stables.
- **Code en anglais, documentation en français.**

## 3. Spécifications

### 3.1 Domaine pur (`core/domain/`, bibliothèque standard uniquement)

- `platforms.py` : `Platform` (StrEnum des 8 valeurs), `validate_external_id(platform,
  value)` (X : chiffres, au plus 19, ≤ 9 223 372 036 854 775 807 ; autres : 1 à 256
  caractères imprimables) ; erreurs du catalogue.
- `text.py` :
  - `normalize(text)` : NFKC, minuscules, suppression des paramètres de suivi des
    URL (`utm_*`, `fbclid`, `gclid`, `ref_src`…), mentions remplacées par `@user`,
    espaces compactés, emojis décoratifs retirés ;
  - `useful_word_count(text)` ;
  - `content_hash(normalized) -> bytes` (SHA-256) ;
  - `simhash64(normalized) -> int` : tokens et bigrammes, hachage 64 bits
    `hashlib.blake2b(digest_size=8)`, valeur convertie en entier signé pour
    PostgreSQL ;
  - `simhash_bands(value) -> tuple[int, int, int, int]` (4 × 16 bits) ;
  - `hamming(a, b)`.
- `language.py` : `detect_language(text, hint=None) -> tuple[str, float]`. Si
  l'indice de la plateforme est valide, il prime. Sinon, d'abord l'écriture (arabe,
  cyrillique, hébreu, grec, CJK, dévanagari, thaï…), puis les mots outils pour fr,
  en, es, de, it, pt, nl. Renvoie `und` si incertain. Tests sur des phrases de
  chaque langue et des cas courts ou mixtes.
- `privacy.py` : `installation_pseudonym(secret, token) -> bytes` (HMAC-SHA256
  tronqué à 16 octets) ; validation du format du jeton (base64url, 22 à 64
  caractères).
- `llm.py` (domaine du routeur) :
  - `Level` (N0 à N4) ;
  - `cache_key(role, provider, model, prompt_version, params, input) -> str` :
    SHA-256 d'un JSON canonique, clés triées, séparateurs compacts ;
  - `estimate_tokens(text_chars, max_output_tokens)` : heuristique documentée ;
  - `cost_usd_micros(usage, prices)` : prix stockés en dollars par million de
    tokens, donc coût en micro-dollars = tokens × prix ;
  - `free_allowance(tier, group)` : paliers 1-2 → 2 500 000 et 250 000 ; paliers 3-5
    → 10 000 000 et 1 000 000 ;
  - `can_use_free(used, estimate, allowance, margin=0.10)`.
- `net.py` : `is_public_ip(ip)` (refuse privé, loopback, lien local, multicast,
  réservé, IPv4 mappé, CGNAT 100.64/10) pour la protection SSRF.

### 3.2 Ports (`core/ports/`)

Contrats en `Protocol` et `dataclass(frozen=True, slots=True)`, sans import lourd.

- `LLMRequest(role, level, prompt_id, input, schema: type[BaseModel],
  max_output_tokens, budget_scope)` et `LLMResult(output, provider, model, usage,
  cost_usd_micros, free_tier, cache_hit, request_id)`.
- `LLMProvider` (ce qu'implémente un adaptateur) : `complete(model, system,
  messages, json_schema, max_output_tokens, params) -> ProviderResponse(output_json,
  usage, request_id, stop_reason)` ; exceptions typées : `ProviderRefusal`,
  `ProviderRetryable`, `ProviderInvalidOutput`, `ProviderFatal`.
- `BatchLLMClient` : contrat seulement (soumettre, relever).
- `Embedder.embed(texts, dimensions) -> list[list[float]]` (+ modèle, version).
- `KnowledgeSource` et `Evidence` (voir `docs/factcheck/connaissances.md`) : contrat
  seulement.
- `SearchClient.search(query, *, limit, lang=None, country=None,
  include_domains=(), exclude_domains=())` : contrat.
- `WebFetcher.fetch(url) -> FetchedPage(url, final_url, status, content_type, text,
  sha256, fetched_at)`.
- `PlatformClient` : signatures de `docs/factcheck/plateformes-medias.md` ;
  `XClient` reste un alias compatible.

### 3.3 Adaptateurs (`core/adapters/`, imports paresseux)

Place les adaptateurs partagés dans `src/tower/core/adapters/` (les services
n'importent que `tower.core`). Note cette décision dans `docs/decisions.md` et
`docs/architecture.md`.

- `openai_llm.py` : SDK officiel `openai`, API Responses, sortie structurée stricte
  (schéma JSON avec `additionalProperties: false` et tous les champs requis ; écris un
  utilitaire qui rend un schéma pydantic compatible). Effort de raisonnement par
  route. Deux clés : `TOWER_OPENAI_API_KEY_SHARED` (organisation avec partage, routes
  `data_sharing_ok`) et `TOWER_OPENAI_API_KEY`. Lecture des tokens (entrée, entrée en
  cache, sortie). **Vérifie les noms exacts des paramètres dans la documentation
  officielle d'OpenAI** (developers.openai.com) : ne devine pas.
- `anthropic_llm.py` : SDK officiel `anthropic`, `client.messages.create` avec
  `output_config={"format": {"type": "json_schema", ...}}` et `output_config.effort`.
  Pas de `temperature`, `top_p` ni `top_k`. Pas de `tool_choice` forcé. Opus 5.5 :
  pas de désactivation de la réflexion ; Sonnet 5.5 : effort fixé explicitement.
  `stop_reason == "refusal"` → `ProviderRefusal`. Cache de prompt sur le système.
  **Utilise la skill `claude-api`** pour les formes exactes du SDK.
- `openai_embedder.py` : `text-embedding-3-small`, `dimensions=512`, lots.
- `http_fetcher.py` : httpx en flux, plafond de 2 Mo, délai, au plus 5
  redirections, résolution DNS et `is_public_ip` à chaque saut, types de contenu
  autorisés (HTML, texte, JSON, XML, PDF en octets), respect de `robots.txt` (cache
  borné), User-Agent `TOWER_HTTP_USER_AGENT` (URL de contact générique, jamais une
  adresse personnelle).
- Tests : construction des requêtes et lecture des réponses via des faux clients
  injectés. Aucun réseau.

### 3.4 Routeur de modèles (`core/llm/`)

`LLMRouter.complete(session_factory, request) -> LLMResult` :

1. Résoudre la route active (`llm_routes` : rôle, niveau ou `*`, modèle principal,
   replis ordonnés, paramètres, `data_sharing_ok`, version).
2. Version du prompt par le registre (`core/llm/prompts.py` : fichiers
   `src/tower/services/<service>/prompts/<prompt_id>.md`, version = 12 premiers
   caractères du SHA-256 du contenu).
3. Clé de cache ; si `llm_calls` contient un succès pour cette clé, le renvoyer
   (`cache_hit=True`, coût 0).
4. Estimer les tokens. Si le modèle a un groupe gratuit et que `can_use_free` est
   vrai (compteur `llm_free_usage` du jour UTC), il est servi en gratuit. Sinon, on
   passe au premier repli payant de la route.
5. Pour un appel payant, **réserver** le coût estimé dans `budget_counters` : un
   seul `UPDATE … SET reserved = reserved + :est WHERE spent + reserved + :est <=
   cap RETURNING …` par portée (`global` et la portée de la requête). En cas d'échec,
   lever l'erreur `TWR-LLM` de budget épuisé, que les jobs traduisent en
   `RetryLater` jusqu'au début de la période suivante.
6. Appeler le fournisseur, sans transaction ouverte pendant l'appel réseau.
7. Valider avec le modèle pydantic. Sortie invalide : une nouvelle tentative, puis
   repli suivant. Refus : repli suivant. Erreur relançable : remonter (le job
   relance).
8. Dans **une** transaction : insérer `llm_calls` (clé de cache unique, `ON
   CONFLICT DO NOTHING`), régler la réservation (coût réel), incrémenter
   `llm_free_usage`, publier `budget.threshold_reached` au premier franchissement de
   50, 80 ou 100 % d'une portée sur la période.

`tower llm seed` insère ou met à jour les modèles (tarifs de
`docs/factcheck/modeles.md`, date de relevé 2026-11-10) et les routes par défaut des
rôles `extract`, `pair_arbiter`, `light_check`, `query_writer`, `narrative_title`,
`frame`, `judge`, `second_opinion`, `objection`, `critic`, `writer`, `auditor`,
`embed`, ainsi que le budget `global` de 30 $ par mois. `tower llm usage` affiche la
consommation gratuite du jour et la dépense du mois par portée.

Tests (vraie base PostgreSQL, fournisseurs factices) :
- le second appel identique est servi par le cache sans appel ;
- l'allocation gratuite bascule vers le repli **avant** dépassement ;
- deux réservations concurrentes sur deux connexions ne dépassent jamais le
  plafond ;
- sortie invalide → nouvelle tentative → repli ;
- refus → repli ;
- événements aux seuils, une seule fois par seuil et par période ;
- les versions (prompt, route) sont bien enregistrées ;
- le budget épuisé produit le bon code.

### 3.5 Ingestion (lot 2)

Schéma (voir `docs/factcheck/donnees.md` et `plateformes-medias.md`) :

- `content_items` : clé interne `BIGINT`, `public_id UUID`, `platform` (CHECK),
  `external_id TEXT`, `UNIQUE (platform, external_id)`, CHECK de format pour X,
  index sur expression `((external_id::bigint)) WHERE platform = 'x'`, compte auteur,
  texte d'origine, `lang`, `lang_confidence`, `lang_hint`, `useful_words`,
  `content_hash` (32 octets, indexé), `simhash BIGINT` et quatre bandes en colonnes
  générées stockées indexées, `parent_external_id`, `quoted_external_id`,
  `copy_of_id`, métriques (JSONB) et leur date, `created_at_platform`,
  `first_seen_at`, `last_seen_at`, `observation_count`. **Pas de partitionnement.**
- `observations` partitionnée par semaine sur `observed_at` (clé primaire incluant
  `observed_at`), avec contenu, source (`extension`, `radar`, `feed`), pseudonyme
  d'installation (16 octets, nullable), version des filtres. Le service `ingest`
  crée les partitions des deux semaines suivantes et supprime celles de plus de
  30 jours après agrégation dans `observation_counters` (contenu, heure, source,
  nombre).
- `installation_secrets(day, secret)` : le secret du jour et celui du lendemain sont
  créés par la maintenance ; ceux de plus d'un jour sont **supprimés**. Note dans la
  doc que les sauvegardes de la base doivent avoir une rétention courte pour que la
  destruction soit réelle.
- `installation_daily(day, pseudonym, items, quota)` : quotas par jour.
- `entities`, `platform_accounts`, `watched_accounts` (catégorie de rôle public,
  priorité, curseur `since`, actif, ajouté par), `content_snapshots`, `media_assets`
  (type, URL, `sha256` et `phash` nullables), `content_media`,
  `extension_filter_versions` (version, filtres en JSONB, créé le, actif).

Cas d'usage (`core/usecases/ingestion.py`, `watchlist.py`) :
- `ingest_items(actor, items, filter_version, installation_token=None)` :
  - validation ;
  - normalisation et empreintes (domaine pur) ;
  - `INSERT … ON CONFLICT (platform, external_id) DO UPDATE` sur les métriques et
    `last_seen_at` ;
  - une observation par item ;
  - quotas par pseudonyme du jour ;
  - événement `content.ingested` **seulement pour les nouveaux contenus** ;
  - job `ingest.link_copies` pour les nouveaux contenus ;
  - retourne les compteurs (acceptés, nouveaux, doublons, refusés).
- Liste de suivi : lecture, ajout, modification. Le rôle `editor` est requis pour
  écrire. Audit et événements `watchlist.changed`.
- Nouvelles permissions à ajouter au modèle existant (`docs/auth.md`) :
  `content:ingest`, `watchlist:read`, `watchlist:write`, et l'acteur `installation`
  avec la seule permission `content:ingest`.

API (gate) :
- `POST /v1/ingest/observations` : en-tête `X-Installation-Token` (extension) ou
  token de service avec `content:ingest` (radar plus tard). Corps : `filter_version`
  et `items` (au plus 50 ; corps ≤ 256 Ko ; champs : `platform`, `external_id`,
  `author {external_id, handle}`, `text`, `lang_hint`, `created_at`, `metrics`,
  `parent_external_id`, `quoted_external_id`, `media [{kind, url}]`,
  `observed_at`). Réponse `202` avec les compteurs. Limite par IP en mémoire (seau à
  jetons, au plus 10 000 entrées, jamais persisté) et quotas par pseudonyme en base.
  IDs en chaînes.
- `GET /v1/ingest/config` : version et filtres actifs (mots-clés, comptes,
  seuils), avec ETag et cache public.
- `GET/POST/PATCH /v1/watched-accounts` : staff.
- Erreurs typées `TWR-ING-*` : jeton invalide, quota dépassé (429), lot trop gros,
  plateforme inconnue, identifiant invalide.

Service `ingest` (`src/tower/services/ingest/`) :
- job `ingest.link_copies` (file `ingest`, idempotent) : même `content_hash`, ou
  SimHash à distance ≤ 3 via les bandes sur 30 jours ; `copy_of_id` reçoit le plus
  ancien ;
- tâches de maintenance singleton : partitions, secrets, agrégation des
  observations.

Tests :
- domaine : chaque fonction, cas limites ;
- intégration : lot nominal, doublons, mise à jour des métriques, quotas,
  rotation des secrets (après suppression, aucun lien possible entre deux jours),
  partitions créées et purgées, liaison des copies (exactes et quasi), permissions
  de la liste de suivi, endpoint de configuration avec ETag, isolation des imports.

### 3.6 Codes, configuration, événements, documentation

- Codes : domaines `TWR-LLM-*` et `TWR-ING-*` dans `core/domain/error_codes.py`,
  puis régénérer `docs/errors.md`.
- Variables : `TOWER_OPENAI_API_KEY`, `TOWER_OPENAI_API_KEY_SHARED`,
  `TOWER_ANTHROPIC_API_KEY`, `TOWER_OPENAI_USAGE_TIER` (défaut 1),
  `TOWER_LLM_FREE_MARGIN` (0.10), `TOWER_HTTP_USER_AGENT`, `TOWER_FC_LANGUAGES`
  (`fr,en`), `TOWER_INGEST_MAX_ITEMS` (50), `TOWER_INGEST_DAILY_QUOTA` (500). Les
  secrets sont lus par la configuration, jamais journalisés. À ajouter dans
  `docs/configuration.md` et `.env.example`.
- Événements dans `docs/events.md` : `budget.threshold_reached`,
  `content.ingested`, `watchlist.changed`.
- Documentation :
  - nouvelles pages `docs/llm.md` (routeur, routes, cache, budgets, allocation
    gratuite, prompts) et `docs/ingestion.md` (API, quotas, vie privée, partitions,
    copies) ;
  - mises à jour de `database.md`, `api.md`, `auth.md`, `services.md`,
    `architecture.md`, `configuration.md`, `events.md`, `roadmap.md`,
    `docs/factcheck/feuille-de-route.md` (lots 1 et 2 marqués livrés, écarts
    expliqués) et `docs/README.md` ;
  - `docs/decisions.md` pour toute décision durable ;
  - résumé de session `docs/sessions/AAAA-MM-JJ-partie-1-fondations-ingestion.md`.

## 4. Méthode de travail (efficacité)

**Branche** : pars de `main` (le plan y est fusionné) et travaille sur la branche que
ta session t'indique. Pas de pull request sans demande explicite de Jules.

**Base de test** : PostgreSQL 16 réel (voir `docs/development.md` ; défaut des tests
`postgresql://postgres@localhost:5433/tower_test`). Installe-le s'il manque. Ne simule
jamais la base.

**Phase A — Contrats (toi seul, avant tout sous-agent)**
1. Vérifie que les 51 tests existants passent.
2. Écris les contrats qui fixent les frontières : ports, signatures du domaine
   (corps `NotImplementedError` si besoin), modèles SQLAlchemy et **les deux
   migrations** (`0002` pour le LLM et les budgets, `0003` pour l'ingestion), codes
   d'erreur réservés, noms des variables, permissions.
3. Vérifie `alembic check`, `ruff` et `mypy`, puis commit (« Add contracts for… »).

C'est ce qui permet ensuite de paralléliser sans conflit.

**Phase B — Sous-agents Claude Sonnet 5.5 en parallèle**

Lance les sous-agents avec le modèle **Claude Sonnet 5.5** (`claude-sonnet-5-5` ;
dans l'outil Agent, `model: "sonnet"`). Vérifie au premier lancement que c'est bien
Sonnet 5.5 qui répond.

Ils codent aussi bien que toi quand la consigne est très précise. Chaque brief doit
donc être **autonome** (ils ne voient pas cette conversation) et contenir :
- les fichiers qu'il crée ou modifie, et **seulement** ceux-là ;
- les signatures exactes ;
- le comportement attendu et les cas limites ;
- les tests à écrire ;
- les commandes à lancer : `uv run ruff format`, `uv run ruff check`,
  `uv run mypy`, `uv run pytest` sur ses tests ;
- les interdits : aucun commit, aucun fichier hors liste, aucun import lourd au
  niveau module, aucun réseau ;
- un rapport final court : fichiers touchés, résultats des commandes, doutes.

Découpage proposé, en lots de fichiers disjoints :

| Agent | Contenu | Dépend de |
|---|---|---|
| S1 | Domaine pur (`platforms`, `text`, `language`, `privacy`, `llm`, `net`) + tests | contrats |
| S2 | Adaptateurs OpenAI, Anthropic, embeddings + tests (faux clients) | contrats |
| S3 | `WebFetcher` httpx + tests | contrats, `net` de S1 (ou réécrit localement dans le brief) |
| S4 | Routeur, registre de prompts, CLI `llm`, tests d'intégration | S1 |
| S5 | Cas d'usage d'ingestion, liste de suivi, permissions, endpoints, tests | S1 |
| S6 | Service `ingest` (copies, partitions, secrets) + tests | S1, S5 |

Lance S1, S2 et S3 ensemble, puis S4 et S5, puis S6. Pendant qu'ils travaillent,
rédige la documentation. N'exécute pas toi-même ce que tu as délégué : relis les
diffs, lance les tests et corrige par un nouveau brief court si nécessaire.

**Phase C — Intégration (toi)**

1. Suite complète : `ruff format --check`, `ruff check`, `mypy` strict, `pytest`,
   `alembic check`, `tower migrate`, puis `--downgrade` et à nouveau `migrate`.
2. Smoke test des vrais processus :
   - gate et hangar avec `TOWER_SERVICES=ping,ingest` ;
   - envoi d'un lot à `/v1/ingest/observations` ;
   - `content.ingested` visible en SSE ;
   - copie liée par le job ;
   - `tower llm seed` puis `tower llm usage`.
3. Mesures RSS (avant et après) et test d'isolation des imports.
4. Relecture de cohérence de la documentation, régénération de `docs/errors.md`.
5. Résumé de session (résultats réels, échecs compris, ce qui n'a pas pu être
   vérifié), décisions, commits en anglais à l'impératif, push.

## 5. Rapport final attendu

En français, court : ce qui est livré, les vérifications avec leurs chiffres (tests,
RSS avant et après), ce qui n'a pas pu être vérifié, les écarts au plan et leur
raison, les questions pour Jules, et la prochaine partie proposée (lot 3 : MVP sur les
comptes suivis, qui demande les clés d'API et le jeu de référence).
