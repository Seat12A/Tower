# 2026-11-25 — API de lecture des cases pour le site

## Objectif

Demande de Jules : donner au site de quoi afficher les cases. Il faut un endpoint de
statistiques, un détail complet des cases et une recherche avec filtres, **paginée**.

## Réalisé

- `core/usecases/case_views.py` (nouveau, lecture seule) :
  - `search_cases` : filtres `q`, `kind`, `status`, `veracity`, `claim_type`,
    `context_need`, `lang`, `jurisdiction`, `platform`, `since`, `until` ; tri `newest`,
    `oldest` ou `relevance` ; pages numérotées avec le total.
  - `case_stats` : comptes par type, véracité, type d'affirmation, langue, juridiction,
    statut (équipe seulement) et chronologie par jour, semaine ou mois. Mêmes filtres que
    la recherche.
  - `get_case_details` : post, affirmation, verdict, note dans la meilleure langue,
    nombre de posts qui portent l'affirmation, chronologie des statuts, bloc interne pour
    l'équipe.
- `gate/routes/case_views.py` (nouveau) : `GET /v1/cases/search`, `GET /v1/cases/stats`,
  `GET /v1/cases/{public_id}/details`. Le routeur est monté avant `cases.router`, sinon
  `search` et `stats` seraient lus comme un id de case. Les réponses anonymes portent
  `Cache-Control: public, max-age=300`.
- `gate/schemas.py` : `CaseSummaryOut`, `CasePageOut`, `CaseDetailsOut` et ses blocs,
  `CaseStatsOut`.
- `core/usecases/drafts.py` : `_public_note` et `_norm_lang` deviennent `public_note` et
  `norm_lang`, réutilisés ici. La règle de note reste la même que pour l'extension.
- Nouveau code d'erreur `TWR-DOS-008` (recherche invalide : `since` après `until`, ou
  page trop profonde). `docs/errors.md` est régénéré.
- Documentation : `docs/api.md` (nouvelle section « Lecture pour le site ») et
  `docs/decisions.md`.
- Tests : `tests/test_gate_case_views.py` (5 tests). Ils couvrent la recherche publique,
  chaque filtre, la pagination et les erreurs, la vue équipe, le détail public, équipe
  et en français, le 404 d'un case non publié, et les statistiques publiques et équipe.

## Décisions

Toutes sont reportées dans `docs/decisions.md` (section du 2026-11-25).

- Le public ne voit que les cases publiés et les verdicts validés. Le filtre est posé
  dans la jointure SQL : un brouillon ne sort ni dans un résultat, ni dans un filtre, ni
  dans un compte.
- Un verdict périmé ou remplacé reste affiché, avec `verdict_state` et sa date de
  validation. C'est au site de le signaler comme ancien.
- Ces réponses ne contiennent ni l'auteur, ni le texte du post, ni le raisonnement
  interne.
- La pagination se fait par pages numérotées avec le total. La profondeur est plafonnée
  à 10 000 lignes.
- La recherche texte est calculée à la volée, sans index ni migration.
- Pas de limite de débit : on s'appuie sur le cache public.

## Vérifications

- `ruff format`, `ruff check` et `mypy` strict sur tout le code : OK.
- `pytest tests/test_gate_case_views.py` : 5 réussis.
- Suite complète : 2 253 réussis, 3 échecs. Les 3 échecs se produisent aussi sur la base
  `a3a3232` sans les changements de ce lot ; ils ne viennent donc pas de ce travail.
  - `test_source_standing.py::test_unrated_source_without_first_party_cannot_be_primary` :
    le verdict n'est pas rétrogradé. La règle des deux sources établies (session du
    2026-11-23) a probablement rendu ce test obsolète.
  - `test_source_standing_db.py::test_identity_refresh_errors` et
    `::test_subject_sites_failure_does_not_stop_the_investigation` : un compte attendu
    vaut 0 au lieu de 1.
- Pas de migration, donc pas de `alembic check` ni d'aller-retour à refaire.
- Smoke test du vrai processus `tower gate` sur une base migrée vide : `search`
  (page 2), `stats?interval=week` et `details` (404 `TWR-DOS-001`) répondent comme
  prévu.
- RSS de gate à 8 s, deux mesures, base `a3a3232` dans un worktree :
  - Au repos : 105,7 et 100,8 Mo avant, 102,4 et 101,5 Mo après. L'écart reste dans le
    bruit, sous la cible de +5 Mo.
  - Après 30 recherches et 30 statistiques : 111,8 et 108,6 Mo. La base n'a pas ces
    routes, ses valeurs ne sont donc pas comparables.
- Aucun appel externe réel : ces routes ne lisent que PostgreSQL.

## Points ouverts / suite

- Corriger les 3 tests `test_source_standing*` qui échouent déjà sur `main`.

- `TOWER_CORS_ORIGINS` doit contenir le domaine du site pour que le navigateur puisse
  appeler gate.
- Si le nombre de cases dépasse quelques milliers, ajouter un index GIN plein texte. Il
  demandera probablement une colonne générée.
- Les statistiques et les filtres de date portent sur la date de création du case.
  Si le site veut « publiés par mois », il faudra une colonne `published_at`
  (migration expand).
- Les cases de modération n'ont pas de verdict : `claim`, `verdict` et `note` y valent
  `null`.
