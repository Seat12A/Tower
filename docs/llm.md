# Routeur de modèles (LLM)

Tout appel de modèle (OpenAI, Anthropic) passe par le **routeur** (`core/llm/`). Il
choisit le modèle selon le rôle, sert les rejeux depuis un cache de contenu, utilise
l'allocation gratuite OpenAI sans jamais risquer sa facturation intégrale, réserve le
budget avant l'appel et journalise chaque appel. Plan et justification :
[factcheck/modeles.md](factcheck/modeles.md), coûts : [factcheck/couts.md](factcheck/couts.md).

## Briques

| Brique | Emplacement | Rôle |
|---|---|---|
| Ports | `core/ports/llm.py` | `LLMRequest`, `LLMResult`, `LLMClient` (ce qu'appellent les services), `LLMProvider` (ce qu'implémente un adaptateur), exceptions `ProviderRefusal`, `ProviderRetryable`, `ProviderInvalidOutput`, `ProviderFatal`, `BatchLLMClient` (contrat seulement) |
| Domaine | `core/domain/llm.py` | Niveaux N0 à N4, clé de cache, estimation des tokens, coût, allocation gratuite, seuils de budget, période mensuelle (pur) |
| Adaptateurs | `core/adapters/openai_llm.py`, `anthropic_llm.py`, `openai_embedder.py`, `factory.py` | SDK officiels importés **paresseusement** (gate et les réplicas sans fact-check ne les chargent jamais) |
| Schéma | `core/llm/schema.py` | `strict_json_schema(Model)` : schéma pydantic rendu compatible avec les sorties strictes (`additionalProperties: false`, tous les champs requis, champs optionnels rendus nullables, `default` retiré) |
| Prompts | `core/llm/prompts.py` | Registre des prompts versionnés par empreinte |
| Routeur | `core/llm/router.py` | `LLMRouter.complete(request)` |
| CLI | `tower llm seed`, `tower llm usage` | Amorçage des modèles, routes et budget ; consommation |

## Tables

Détail des colonnes : [database.md](database.md).

| Table | Rôle |
|---|---|
| `llm_models` | Fournisseur, modèle, tarifs en $ par million de tokens (entrée, entrée en cache, sortie, facteur lot), groupe d'allocation gratuite (`small`, `large` ou vide), date de relevé |
| `llm_routes` | Rôle, niveau (`N0`..`N4` ou `*`), modèle principal, replis ordonnés, paramètres (`effort`…), `data_sharing_ok`, version ; une seule version active par (rôle, niveau) |
| `llm_calls` | Une ligne par tentative : clé de cache, versions (prompt, route), tokens, coût en micro-dollars, gratuit, latence, ID de requête du fournisseur, sortie JSON, statut (`succeeded`, `invalid_output`, `refused`, `failed`). Index unique partiel sur la clé parmi les succès : c'est le **cache** |
| `llm_free_usage` | Tokens consommés par (fournisseur, groupe, jour UTC) |
| `budget_counters` | Plafond, dépensé, réservé par (portée, période) ; seuils déjà notifiés |

## Déroulé d'un appel

`LLMRouter.complete(LLMRequest)` :

1. **Route** : version active pour (rôle, niveau), sinon (rôle, `*`) ; absente →
   `TWR-LLM-002`. Candidats = modèle principal puis replis, dans l'ordre.
2. **Prompt** : texte et version (12 premiers caractères du SHA-256 du fichier) ;
   inconnu → `TWR-LLM-003`. Le message utilisateur contient l'entrée en JSON canonique
   dans un bloc délimité `<input>…</input>`, présenté comme donnée non fiable (jamais
   dans les instructions).
3. **Cache** : une clé par candidat, `SHA-256(rôle, fournisseur, modèle, version du
   prompt, paramètres, entrée)` en JSON canonique. Un succès existant pour l'une des
   clés est renvoyé sans appel (`cache_hit=True`, coût 0). Un changement de prompt ou
   de paramètres invalide naturellement le cache.
4. **Clé d'API** : Anthropic → `TOWER_ANTHROPIC_API_KEY`. OpenAI → organisation avec
   partage (`TOWER_OPENAI_API_KEY_SHARED`) si la route est `data_sharing_ok`, sinon
   organisation sans partage (`TOWER_OPENAI_API_KEY`). Clé absente → candidat suivant,
   puis `TWR-LLM-008` si aucun candidat n'est utilisable.
5. **Allocation gratuite** : seulement pour un modèle à groupe gratuit appelé avec la
   clé partagée. Le routeur estime les tokens (`ceil(caractères / 3) + 50` en entrée,
   `max_output_tokens` en sortie) et **réserve** l'estimation dans `llm_free_usage`
   par une mise à jour conditionnelle : `reste ≥ estimation × (1 + marge)`
   (`TOWER_LLM_FREE_MARGIN`, 10 %). Allocation : 2,5 M (petits) et 250 000 (grands)
   tokens par jour aux paliers 1-2, 10 M et 1 M aux paliers 3-5
   (`TOWER_OPENAI_USAGE_TIER`). Si la réservation échoue, ou si la clé partagée n'est
   pas configurée, le candidat est **sauté** : un modèle à groupe gratuit n'est jamais
   appelé en payant (certains sont chers hors allocation, comme `gpt-5.6-terra`).
6. **Budget** (appel payant) : coût estimé réservé dans `budget_counters` pour la
   portée `global` et la portée de la requête (si elle a un compteur), dans une
   transaction : `UPDATE … SET reserved = reserved + :est WHERE spent + reserved + :est
   <= cap`. Une portée qui refuse annule toute la réservation et le routeur passe au
   candidat suivant (un repli moins cher peut encore tenir dans le budget). Si aucun
   candidat n'a pu être tenté → `BudgetExhausted` (`TWR-LLM-001`), que les jobs
   traduisent en `RetryLater` jusqu'au début de la période suivante ; si seule
   l'allocation gratuite manquait, la portée est `free_allowance` et la relance a lieu
   au prochain minuit UTC. La réservation reste exacte avec plusieurs réplicas (testé
   avec deux connexions).
7. **Appel** du fournisseur, **sans transaction ouverte**.
8. **Validation** pydantic. Sortie invalide : une nouvelle tentative sur le même
   modèle, puis candidat suivant. Refus : candidat suivant. Erreur relançable (quota,
   délai, 5xx) : remontée (`TWR-LLM-006`), le job relance. Erreur fatale (clé,
   requête) : remontée (`TWR-LLM-007`).
9. **Règlement**, dans une transaction : ligne `llm_calls` (insertion sans effet si un
   succès de même clé existe déjà), coût réel imputé et réservation libérée,
   ajustement de `llm_free_usage` au réel, et `budget.threshold_reached` publié au
   **premier** franchissement de 50, 80 ou 100 % d'une portée sur la période (une
   seule fois par seuil, même avec plusieurs réplicas).

Tous les candidats épuisés : `TWR-LLM-004` si le dernier échec est un refus,
`TWR-LLM-005` sinon. Chaque tentative, même échouée, est journalisée et facturée.

Limites connues : si deux réplicas obtiennent le même succès en même temps, le
second coût est bien imputé au budget mais une seule ligne `llm_calls` est gardée ; une
sortie en cache qui ne valide plus le schéma est marquée `invalid_output` (le cache est
libéré).

## Coûts

Prix en dollars par million de tokens, donc coût en micro-dollars = tokens × prix
(arrondi au micro-dollar supérieur), avec les tokens d'entrée en cache au tarif
« entrée en cache ». Chez Anthropic, l'écriture dans le cache de prompt (facturée
1,25 × l'entrée) est comptée au tarif d'entrée normal : légère sous-estimation
assumée. Les appels gratuits coûtent 0.

## Budgets

Un compteur par (portée, période mensuelle, du 1er au 1er en UTC). Le compteur du mois
est créé à la première réservation à partir du plafond du mois précédent de la même
portée ; une portée sans aucun compteur n'est pas plafonnée (seul `global` s'applique).
`tower llm seed` crée `global` à 30 $ par mois. Modifier un plafond :

```sql
UPDATE budget_counters SET cap_usd_micros = 50000000
WHERE scope = 'global' AND period_start = date_trunc('month', now())::date;
```

Ou en ligne de commande : `tower llm budget <portée> <usd>` (plafond du mois courant,
compteur créé s'il manque). `tower llm seed` crée aussi `provider:x`
(`TOWER_X_MONTHLY_CAP_USD`, 20 $) s'il n'existe aucun compteur pour cette portée.
Consultation : `tower llm usage`, `GET /v1/llm/usage` et `/usage` dans Discord.

### Réservations et récupération des réservations orphelines

Le registre commun `core/budget.py` sert au routeur et au radar :

1. `reserve_budget` : dans sa propre transaction, `reserved += estimation` sur chaque
   portée plafonnée (tout ou rien), et une ligne `budget_reservations` par compteur
   tenu, avec `expires_at = maintenant + TOWER_BUDGET_RESERVATION_TTL_S` (30 min).
2. L'appel externe, sans transaction ouverte.
3. `settle_budget` (transaction de l'appelant) : supprime les lignes de réservation ;
   seules les lignes **réellement supprimées** rendent leur montant à `reserved`
   (aucun double décrément si la maintenance est passée entre-temps) ; `spent +=
   coût réel` dans tous les cas ; seuils publiés comme ci-dessous.

L'allocation gratuite suit le même principe (`reserve_free`, `settle_free`, ligne
`kind = 'free'`). La tâche de maintenance singleton `tower:budget:release` (toutes les
60 s, dans tout hangar) supprime les réservations expirées (`FOR UPDATE SKIP LOCKED`)
et rend leur montant ; elle journalise et stocke `TWR-MNT-002` (avertissement) quand
elle libère quelque chose : signe qu'un processus est mort entre réservation et
règlement.

**Portées.** `global` est le budget des modèles ; le radar réserve sur `provider:x`
seul (le coût de X ne consomme pas le budget des modèles). Effets prévus des seuils (escalade Opus
suspendue à 80 %, etc.) : à la charge des étages du fact-check, à partir de
l'événement et des compteurs.

## Routes par défaut (`tower llm seed`)

Idempotent : met à jour les tarifs des modèles connus (sans jamais écraser un
`free_group` déjà renseigné), crée la version 1 d'une route
seulement si aucune route n'existe pour (rôle, niveau) — une route modifiée par
l'équipe n'est jamais écrasée. Tarifs relevés le 2026-11-10
([factcheck/modeles.md](factcheck/modeles.md)).

| Rôle | Niveau | Principal | Replis | Paramètres | Partage |
|---|---|---|---|---|---|
| `extract` | `*` | `gpt-5.6-luna` | `gpt-6-luna`, `claude-haiku-5-5` | effort `low` | oui |
| `pair_arbiter` | `*` | `gpt-5-nano` | `gpt-6-luna` | effort `low` | oui |
| `light_check` | `*` | `gpt-5.6-terra` | `gpt-6.1-terra`, `claude-haiku-5-5` | effort `low` | oui |
| `query_writer` | `*` | `gpt-5.6-terra` | `gpt-6-luna` | effort `low` | oui |
| `narrative_title` | `*` | `gpt-5.6-luna` | `gpt-6-luna` | effort `low` | oui |
| `frame` | `*` / `N4` | `claude-sonnet-5-5` / `claude-opus-5-5` | — / `claude-sonnet-5-5` | effort `medium` | non |
| `judge` | `*` / `N4` | `claude-sonnet-5-5` / `claude-opus-5-5` | — / `claude-sonnet-5-5` | effort `high` / `medium` | non |
| `second_opinion`, `objection`, `critic` | `*` | `gpt-5.6-sol` | `gpt-6.1-terra` | effort `medium` | oui |
| `writer` | `*` | `claude-sonnet-5-5` | `claude-haiku-5-5` | effort `medium` | non |
| `auditor` | `*` | `claude-fable-5-2` | `claude-opus-5-5` | effort `high` | non |
| `embed` | `*` | `text-embedding-3-small` | — | `dimensions` 512 | non |

Groupes gratuits : `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5-mini`, `gpt-5-nano`
(`small`) ; `gpt-5.6-sol` (`large`). La famille GPT-6 n'est **pas** marquée gratuite
tant que Jules ne l'a pas vérifiée dans le tableau de bord OpenAI : la marquer revient
à renseigner `free_group` dans `llm_models`.

## Particularités des adaptateurs

- **OpenAI** (API Responses) : `instructions` (prompt système), `input`, sortie
  stricte `text.format = {type: json_schema, name, schema, strict: true}`,
  `reasoning.effort` si la route le précise, `max_output_tokens`, `store: false`.
  Refus : partie de contenu `refusal`. Réponse `incomplete` → sortie invalide.
  Tokens : `input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens`.
- **Anthropic** (Messages) : `output_config = {format: {type: json_schema, schema},
  effort}`, prompt système avec `cache_control` éphémère. Jamais `temperature`,
  `top_p`, `top_k`, `thinking` ni `tool_choice` (Opus 5.5 ne désactive pas la
  réflexion ; l'effort est le seul réglage). `stop_reason == "refusal"` →
  `ProviderRefusal` ; `max_tokens` → sortie invalide. Le repli côté serveur
  (`fallbacks`) n'est pas utilisé : le routeur fait le repli lui-même pour connaître
  le modèle réellement facturé.
- **Embeddings** : `text-embedding-3-small`, `dimensions=512`, lots de 256 textes.
  Utilisé à partir du lot 4 par le service d'embeddings (`core/llm/embeddings.py`) : route
  `embed`, budget réservé sur `global` et sur le niveau du contenu, un `llm_calls` par
  requête (sans sortie), cache mémoire borné par processus. Doublure `FakeEmbedder`
  avec `TOWER_LLM_FAKE=1`. Le rôle `pair_arbiter` (E3) passe par le routeur comme les
  autres (prompt `services/factcheck/prompts/pair_arbiter.md`).

## Prompts

Fichiers `src/tower/services/<service>/prompts/<prompt_id>.md`, en anglais, livrés
dans le paquet. `prompt_id` est unique dans tout le dépôt (doublon refusé au
chargement). Version = 12 premiers caractères du SHA-256 du contenu, enregistrée dans
chaque `llm_calls`. Aucun prompt de production n'est livré au lot 1 : ils arrivent avec
les étages (lot 3).

## CLI

```bash
uv run tower llm seed     # modèles, routes par défaut, budget global (idempotent)
uv run tower llm usage    # allocation gratuite du jour, dépense du mois par portée
```

## Utilisation depuis un service

```python
router = LLMRouter(ctx.db, build_llm_providers(ctx.settings), PromptRegistry.default(),
                   usage_tier=ctx.settings.openai_usage_tier,
                   free_margin=ctx.settings.llm_free_margin)
try:
    result = await router.complete(LLMRequest(role="extract", level=Level.N1,
        prompt_id="extract", input={"text": ...}, schema=Extraction,
        max_output_tokens=800, budget_scope="level:N1"))
except LLMRouterError as exc:
    raise to_job_error(exc)  # RetryLater pour budget épuisé ou fournisseur indisponible
```

Le routeur est sans état (tout est en base) : un réplica en crée un par processus.
