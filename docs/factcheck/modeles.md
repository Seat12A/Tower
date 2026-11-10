# Modèles : routage OpenAI et Anthropic

Principe demandé par Jules : **OpenAI pour les petits modèles très sollicités** (tokens
gratuits), **Anthropic pour ce qui est complexe**. Le code ne nomme jamais un modèle :
il demande un **rôle** (`extract`, `judge`…), et un **routeur** choisit le fournisseur,
le modèle et les paramètres selon une table versionnée en base. Changer de modèle,
ajouter un fournisseur ou basculer un rôle se fait sans déploiement.

Tarifs et conditions relevés le 2026-11-10 (sources dans [recherche.md](recherche.md)).
Les tarifs sont stockés en base (`llm_models`) avec leur date de relevé, pour calculer
les coûts sans les coder en dur.

## Le programme de tokens gratuits OpenAI

Une organisation OpenAI qui accepte de **partager ses entrées et sorties d'API** avec
OpenAI reçoit une allocation quotidienne gratuite :

| Groupe de modèles | Paliers d'usage 1-2 | Paliers 3-5 |
|---|---|---|
| Grands modèles (gpt-5.6-sol, gpt-5.x, série o, gpt-4.1, gpt-4o) | 250 000 tokens/jour | 1 000 000 tokens/jour |
| Petits modèles (gpt-5.6-terra, gpt-5.6-luna, variantes mini et nano) | 2 500 000 tokens/jour | 10 000 000 tokens/jour |

Règles connues (source secondaire datée du 27 août 2026, à confirmer sur le tableau de
bord) :

- entrées et sorties comptent ensemble, par groupe, remise à zéro à 00:00 UTC ;
- **une requête qui dépasse le reste de l'allocation est facturée en entier** au tarif
  standard ;
- exclus : modèles affinés, évaluations, **utilisation d'outils** (nous n'en utilisons
  pas : la collecte est hors modèle) ;
- exclus si l'organisation a la rétention zéro ou un contrat Enterprise ;
- un solde positif est nécessaire ; seul le propriétaire de l'organisation active le
  partage ; OpenAI peut arrêter le programme avec 30 jours de préavis ;
- l'éligibilité de la famille GPT-6 (gpt-6-luna, gpt-6.1-terra…) et des API Batch et
  Flex n'est **pas confirmée** : à vérifier dans le tableau de bord (vue Usage groupée
  par niveau de service).

Conséquences pour la conception :

1. **Le routeur compte les tokens** par groupe et par jour (`llm_free_usage`) et estime
   la taille de chaque requête **avant** l'appel. Si le reste de l'allocation est
   inférieur à l'estimation plus une marge de 10 %, il bascule sur le modèle payant de
   repli. On ne risque jamais la facturation intégrale d'une grosse requête.
2. **On ne dépend pas du programme.** Chaque rôle a un repli payant bon marché ; si le
   programme s'arrête, le coût mensuel augmente de quelques dollars (voir
   [couts.md](couts.md)).
3. **Données partagées = données publiques seulement.** Les entrées peuvent servir à
   entraîner les modèles d'OpenAI. On n'y envoie que du contenu public (textes de
   posts publics, pages web publiques), mentions pseudonymisées (`@user`), sans nom
   d'auteur, sans note interne, sans identité de relecteur, sans étiquette politique.
   Point juridique ouvert (RGPD : les posts contiennent des données personnelles
   d'auteurs, et OpenAI les réutilise pour ses propres fins) : voir
   [questions.md](questions.md).
4. **Deux clés OpenAI possibles** : une organisation dédiée avec partage
   (`TOWER_OPENAI_API_KEY_SHARED`) pour les rôles publics, et une organisation sans
   partage (`TOWER_OPENAI_API_KEY`) pour tout ce qui ne doit pas l'être. Chaque route
   déclare `data_sharing_ok`.

### Le flux tient-il dans l'allocation ?

Au scénario supposé, régime établi :

| Rôle | Calcul | Tokens/jour |
|---|---|---|
| Extraction E2 : masse en lots de 10 (préfixe de 900 tokens), N3 en solo, rejeu de 2 % en solo | estimation de Fable | ~1,65 M |
| Arbitre E3 (zone grise + toutes les paires interlingues) | ~700 paires × ~300 | ~0,2 M |
| Vérification légère E5 et variantes de récits | ~90 groupes × ~3 000 | ~0,27 M |
| Requêtes E7, titres de récits, divers | | ~0,1 M |
| **Petits modèles** | | **~2,2 M** (sous 2,5 M au palier bas, marge faible) |
| Second avis E8 (~18 × ~7 000) + objections (~6) | estimation de Fable | **~0,175 M** (sous 250 000) |

Au palier bas, la marge sur les petits modèles est faible : le routeur bascule le
surplus sur le repli payant (quelques centimes par jour). Sans lots et avec l'ancien
préfixe (1 750 tokens par post), l'extraction seule consommerait 6,3 M de tokens par
jour. Au démarrage à froid (classifieur absent, 12 000 posts en E2), le surplus coûte
environ 0,20 $ par jour.

## Rôles et routes par défaut

| Rôle | Étage | Tâche | Route principale | Repli payant | Pourquoi |
|---|---|---|---|---|---|
| `extract` | E2 | Extraire et canoniser les affirmations, signature | OpenAI petit modèle gratuit (candidat : `gpt-5.6-luna`) | `gpt-6-luna` (0,10 / 0,50 $) puis `claude-haiku-5-5` | Tâche courte, structurée, très fréquente ; faux négatifs rattrapés par les autres posts du groupe |
| `pair_arbiter` | E3 | Deux affirmations disent-elles la même chose ? | OpenAI petit (candidat : `gpt-5-nano` ou `gpt-5.6-luna`) | `gpt-6-luna` | Question binaire |
| `light_check` | E5 | Comparer l'affirmation aux éléments Atlas | OpenAI petit plus capable (candidat : `gpt-5.6-terra`) | `gpt-6.1-terra` (1 / 5 $) ou `claude-haiku-5-5` | Signal non publié, sert à prioriser |
| `query_writer` | E7 | Requêtes symétriques par type de source | OpenAI petit (`gpt-5.6-terra`) | `gpt-6-luna` | Faible volume, qualité utile |
| `narrative_title` | vagues | Titre neutre d'un récit | OpenAI petit | `gpt-6-luna` | Trivial |
| `frame` | E6 | Cadrage : prendre du recul | `claude-sonnet-5-5`, effort `medium` (enquête standard) ; `claude-opus-5-5` (profondeur renforcée, N4) | `claude-sonnet-5-5` | Compréhension fine, définitions, sous-entendus |
| `judge` | E8 | Verdict sourcé | `claude-sonnet-5-5` (enquête standard ; API Batch en N2) ; `claude-opus-5-5` (profondeur renforcée, N4) | Opus → Sonnet quand le budget est atteint | Peser des sources contradictoires, distinguer « Trompeur » de « Faux » ; la profondeur dépend de la difficulté, pas du niveau |
| `second_opinion` | E8 | Issue indépendante, sans voir le verdict, passages dans un autre ordre | OpenAI grand modèle gratuit (candidat : `gpt-5.6-sol`) | `gpt-6.1-terra` | **Autre fournisseur que le juge** : erreurs décorrélées, pas d'ancrage sur le verdict, couvre le biais de position |
| `objection` | E8 | Objection typée (liste fermée) en cas de désaccord de fond | même modèle que `second_opinion` | `gpt-6.1-terra` | Ne sert qu'au relecteur et à l'escalade |
| `critic` | E9 | Test de pontage de la note de contexte | OpenAI grand modèle gratuit | `gpt-6.1-terra` | Autre fournisseur que le rédacteur |
| `writer` | E9 | Brouillon et note de contexte dans la langue du post, plus la version française | `claude-sonnet-5-5` | `claude-haiku-5-5` | Rédaction nuancée et neutre |
| `auditor` | hors flux | Audit hebdomadaire des désaccords, cas pièges | `claude-fable-5-2` (8 / 40 $), API Batch | `claude-opus-5-5` | Améliore tous les verdicts futurs, pas un verdict isolé |
| `media_describe` (futur) | M1 | Description et texte d'une image | OpenAI petit à entrée image (candidat : `gpt-6-luna`) | `claude-haiku-5-5` | Volume potentiellement élevé |
| `embed` | E1, E3, E7 | Vecteurs | `text-embedding-3-small`, 512 dimensions | — | 0,02 $ / M tokens, 50 % en lot |

Les candidats marqués « candidat » sont départagés sur le jeu de référence
([evaluation.md](evaluation.md)) avant la mise en service : on retient le moins cher
qui atteint le seuil de qualité du rôle, pas le plus puissant.

Tarifs utiles (par million de tokens, entrée / sortie) : `gpt-5-nano` 0,05 / 0,40 ;
`gpt-5-mini` 0,25 / 2 ; `gpt-5.6-luna` 0,20 / 1,20 ; `gpt-5.6-terra` 2 / 12 ;
`gpt-5.6-sol` 4 / 20 ; `gpt-6-luna` 0,10 / 0,50 ; `gpt-6.1-terra` 1 / 5 ;
`claude-haiku-5-5` 0,10 / 0,50 ; `claude-sonnet-5-5` 2 / 10 ; `claude-opus-5-5`
4 / 20 ; `claude-fable-5-2` 8 / 40. API Batch et Flex : −50 % chez OpenAI ; Batch
−50 % chez Anthropic. Attention : `gpt-5.6-terra` est gratuit dans l'allocation mais
cher hors allocation, d'où un repli vers un autre modèle et non vers lui-même en
payant.

## Contrat du routeur

```python
@dataclass(frozen=True, slots=True)
class LLMRequest:
    role: str                      # "extract", "judge", ...
    level: Level                   # N1..N4: selects the route
    prompt_id: str                 # "judge"; the version comes from the prompt registry
    input: Mapping[str, Any]       # canonical, JSON-serialisable input
    schema: type[BaseModel]        # pydantic model of the expected output
    max_output_tokens: int
    budget_scope: str              # "level:N3", "surge", ...

@dataclass(frozen=True, slots=True)
class LLMResult:
    output: BaseModel              # validated against the schema
    provider: str
    model: str
    usage: TokenUsage              # input, cached input, output
    cost_usd_micros: int           # 0 when served by the free allocation
    free_tier: bool
    cache_hit: bool                # served from llm_calls without a call
    request_id: str
```

Étapes d'un appel :

1. Résoudre la route (rôle, niveau) dans `llm_routes` (version active).
2. Calculer la clé de contenu ; si présente dans `llm_calls`, renvoyer le résultat.
3. Estimer les tokens ; choisir allocation gratuite ou modèle payant ; **réserver** le
   coût estimé dans `budget_counters` (mise à jour atomique conditionnelle : la
   réservation échoue si elle dépasse le plafond, ce qui reste exact avec plusieurs
   réplicas).
4. Appeler l'adaptateur (SDK officiel, importé paresseusement).
5. Valider la sortie contre le schéma ; une sortie invalide est réessayée une fois,
   puis le rôle bascule sur le repli.
6. Enregistrer `llm_calls` (tokens, coût réel, version de prompt, de profil, de route)
   et **régler** la réservation, dans la même transaction.
7. Publier `budget.threshold_reached` au passage de 50 %, 80 % et 100 % d'un budget.

## Particularités des fournisseurs

### OpenAI

- API Responses, sorties structurées par schéma JSON strict.
- Cache de prompt automatique (entrées en cache facturées 10 % du tarif) : les
  instructions forment un préfixe stable, les données variables viennent après.
- Effort de raisonnement réglable (`none` à `max` sur la famille GPT-6) : `none` ou
  `low` pour l'extraction et l'arbitrage.
- API Batch et Flex à −50 % pour les lots N1 quand l'allocation gratuite est épuisée.

### Anthropic

- Sorties structurées via `output_config.format`. Le `tool_choice` forcé est refusé
  sur Opus 5.5 et Sonnet 5.5 : on n'utilise pas d'outil pour obtenir du JSON.
- Opus 5.5 : la réflexion ne se désactive pas, l'effort est le seul réglage (défaut
  `medium`, fixé explicitement). Sonnet 5.5 : effort par défaut `high`, fixé
  explicitement (`low` pour le cadrage N2).
- Ne pas envoyer `temperature`, `top_p` ni `top_k` (refusés par la version d'API
  `2026-10-21`).
- Gérer `stop_reason == "refusal"` (sujets sensibles : terrorisme, santé…) : repli
  côté serveur (`fallbacks: "default"`, en bêta) ou repli du routeur ; un refus est
  journalisé et le groupe est signalé au relecteur.
- Cache de prompt (`cache_control`) sur les instructions et les exemples fixes ;
  exemples dynamiques placés après le dernier point de cache.
- API Message Batches à −50 % pour les juges N2 (latence acceptable : 2 h).

## Prompts

- Stockés dans le dépôt : `src/tower/services/<service>/prompts/<role>.md`, en
  **anglais** (règle de langue : le prompt est du code), avec consigne explicite de
  produire les champs destinés aux lecteurs dans la langue du post et en français
  (voir [international.md](international.md)).
- Version = empreinte du fichier ; chaque appel l'enregistre. Un changement de prompt
  invalide naturellement le cache de contenu.
- Le texte non fiable (post, pages web) est toujours placé dans un bloc délimité et
  marqué, jamais dans les instructions (voir [biais.md](biais.md)).

## Ce que les modèles ne reçoivent jamais

L'auteur, son bord politique, ses métriques d'engagement, le niveau de traitement, le
nom du relecteur. Le juge et le cadrage ne reçoivent pas non plus le texte du post,
seulement l'affirmation canonique. Ces règles sont testées : un test vérifie que les
entrées des rôles `frame`, `judge`, `second_opinion` et `objection` ne contiennent aucun champ
interdit.
