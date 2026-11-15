# 2026-11-15 — `/incidents show` et correctif de la clé d'embeddings

## Objectif
Un fact-check manuel a échoué (`TWR-FCK-019` ×5, `TWR-JOB-006` ×4, `TWR-JOB-002`).
`tower errors list` affiche le code et l'emplacement, mais pas le message. Jules veut
lire le détail d'une erreur depuis le bot.

## Réalisé
- Gate : `GET /v1/errors/{empreinte}` renvoie aussi `hint` et `retryable` du catalogue
  (`src/tower/gate/routes/errors.py`).
- Client intercom : `list_incidents(code=, include_resolved=)`, nouveau `get_incident`
  (`src/tower/intercom/gate_client.py`).
- Handler `incidents.show` (`src/tower/intercom/handlers/incidents.py`) : statut,
  sévérité (couleur de la carte), occurrences, dates, processus, emplacement, exception,
  relançable, note de résolution, dernier message, piste, dernier contexte (12 clés au
  plus, valeurs coupées à 120 caractères) et fin de la dernière trace. Le corps est borné
  pour tenir dans un message Components V2 : la trace prend la place restante. Les
  ```` ``` ```` présents dans les données sont neutralisés.
- `/incidents list` : options `code` (validé `TWR-XXX-NNN`) et `all` (résolus compris),
  menu « voir » en plus du menu « résoudre » (celui-ci ne propose que les incidents ouverts).
- `/incidents show empreinte` (préfixe de 6 caractères accepté, résolus compris), bouton
  « Résoudre » (`t12a:inc:res:<empreinte>`, ouvre la modal de note) et retour à la liste.
- Aide et i18n en/fr, `docs/intercom.md`, `docs/api.md`.
- Docs d'exploitation corrigées : dans la console SSH Railway, la commande est
  `/app/.venv/bin/tower …` (le `PATH` de l'image n'est pas repris) ;
  `docs/error-handling.md` disait encore `railway run`.

- **Correctif** : `runtime.get_embedder` exigeait `TOWER_OPENAI_API_KEY`, alors que la
  clé est facultative depuis le 2026-11-14 (toutes les routes passent par l'organisation
  avec partage, voir `docs/configuration.md`). Les jobs `factcheck.extract` échouaient en
  prod avec `ProviderNotConfigured` (`TWR-JOB-006` ×8 puis `TWR-JOB-002`). La fonction
  passe maintenant par `adapters.factory.build_embedder` : clé sans partage d'abord, sinon
  celle avec partage. Test `test_runtime_embedder_falls_back_to_the_shared_key`.

## Diagnostic de prod (fait avec Jules dans la console SSH)
- 1er échec : `X API 402 on tweets.get` (crédits X épuisés). Jules a acheté des crédits
  et la lecture passe.
- 2e échec : `OpenAI API key is not configured` dans `get_embedder` (correctif ci-dessus).

## Décisions
- Le détail est réservé à `errors:read`, comme la liste. Le contexte est déjà nettoyé des
  secrets par le reporter (`_scrub`).
- La fin de la trace est affichée plutôt que le début, parce que l'exception et la frame
  fautive sont en bas.

## Vérifications
- `ruff format --check`, `ruff check` et `mypy` strict : OK. `docs/errors.md` est à jour.
- Tests sans base : `test_intercom_ui`, `test_intercom_team` et les tests de handlers avec
  un faux gate passent, dont le nouveau test de bornage d'un rapport long (il a révélé un
  dépassement de 4000 caractères, corrigé).
- **Non vérifié localement** : les tests qui demandent une base. Le conteneur n'a que
  pgvector 0.6 (paquet Debian) alors que la migration 0006 exige 0.8, et la compilation
  depuis les sources (`docs/development.md`) a été refusée par le mode de permission de la
  session. Ça concerne `test_incident_show_filters_and_resolved`, la nouvelle assertion de
  `tests/test_gate.py` et `test_incidents_list_and_resolve`. À lancer en CI ou sur un poste
  avec pgvector ≥ 0.8.
- Suite complète (avant le correctif) : 1236 tests réussis, 0 échec, 796 en erreur à la
  préparation (pgvector 0.6). Après le correctif : `test_adapters_llm` (29) et les tests
  d'isolation des imports (37) passent.
- Pas de migration, pas de nouveau code chargé au démarrage de gate ou de hangar (la RAM
  n'a pas été mesurée).

## Points ouverts / suite
- Propositions en attente de Jules : (A) demande manuelle sans lecture si le post est en
  base, sinon oEmbed, et l'API X seulement en secours ; (B) promotion sur les métriques de
  l'extension quand l'API X est indisponible ; carte de suivi en direct des commandes
  longues (dans le salon plutôt qu'en message éphémère).
- Le job `factcheck.request` relance 5 fois une erreur 401/403/4xx de X, ce qui ne sert à
  rien. Il faudrait en faire une `PermanentError` (correctif proposé, pas encore fait).
- `docs/deployment-railway.md` montre encore `railway run --service gate tower …` pour
  `create-service-token`, `staff` et `llm budget` : même problème de base injoignable
  depuis le poste. À aligner sur la console SSH.
