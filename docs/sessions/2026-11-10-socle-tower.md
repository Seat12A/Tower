# 2026-11-10 — Socle de 12A Tower

## Objectif

Construire le « cœur du cœur » de la Tower : l'ossature sur laquelle se brancheront
l'API, le bot, la surveillance X et les services de fact-check et de modération.
Contraintes : scalable de tous les côtés, économe en RAM, propre, déployé sur
Railway. Ajouts en cours de session : gestion d'erreurs avec codes et stockage, logs
lisibles dans Railway, `CLAUDE.md` complet, dossier `docs/`, résumé de session
systématique.

## Réalisé

- Projet Python 3.13 avec uv (`pyproject.toml`, `uv.lock`), ruff, mypy strict, pytest.
- `core/` : config (`config.py`), logs JSON Railway (`logging.py`), base asynchrone
  (`db/`), domaine pur (`domain/` : dossiers, sécurité, erreurs, catalogue de codes),
  modèles, cas d'usage des dossiers, outbox + LISTEN/NOTIFY + consommateurs
  (`events/`), file de jobs + runner + maintenance (`jobs/`), services enfichables
  (`services/`), capture et stockage des erreurs (`errors/`), auth (`auth/`), ports.
- `gate/` : FastAPI (santé, dossiers, SSE, erreurs), middleware d'observabilité
  (request_id, log d'accès, filet `TWR-INT-001`).
- `hangar/` : worker générique ; service `ping` de démonstration.
- Migration Alembic initiale `0001` (8 tables).
- `Dockerfile` multi-étapes, `railway/gate.json`, `railway/hangar.json`, CI GitHub
  Actions avec PostgreSQL 16.
- Documentation complète dans `docs/` et `CLAUDE.md`.

## Décisions

- PostgreSQL seul : file `SKIP LOCKED`, outbox lue par `(xid, id)`, advisory locks.
- Le bot consommera les événements en SSE via gate (jamais la base).
- Codes `TWR-<DOMAINE>-<NNN>`, erreurs agrégées par empreinte, incidents publiés
  pour la sévérité ≥ error.
- Statuts en anglais dans la base (`received`, `in_review`, `validated`,
  `published`, `rejected`), libellés français dans la doc et l'interface.
- Rôles `reviewer`, `editor`, `admin` ; staff = rôles ∩ scopes du service.
- Détails : `docs/decisions.md`.

## Vérifications

- `pytest` : 51 tests verts sur PostgreSQL 16 réel (domaine, outbox, consommateurs,
  file et runner, erreurs, API, SSE avec reprise, aller-retour complet via hangar).
- `ruff format --check`, `ruff check`, `mypy --strict` : OK.
- `tower migrate`, `--downgrade base`, puis à nouveau `migrate`, et `alembic check` : OK.
- Smoke test des vrais processus : `/health`, `/ready`, création de dossier, 404
  typé, `ping.requested` → `ping.done` via hangar, arrêt propre sur SIGTERM. Logs
  JSON conformes au format Railway. RSS au repos : gate ≈ 80 Mo, hangar ≈ 67 Mo ;
  hangar ne charge ni FastAPI ni uvicorn.
- Non vérifié : `docker build` (pas de démon Docker dans l'environnement de session)
  et le déploiement Railway réel.
- À noter : le bac à sable n'a pas d'IPv6 ; le smoke test de gate a été fait avec
  `TOWER_HOST=0.0.0.0` (défaut `::` conservé pour Railway).

## Points ouverts / suite

- Premier déploiement Railway : vérifier le build Docker, `preDeployCommand`, la
  config-as-code par service et la limite de connexions de l'offre PostgreSQL.
- Étape suivante : ingestion (tables `observed_posts`, `observations`,
  `watched_accounts`, `installations`, `post_snapshots`) et endpoint de l'extension.
- Cycle de vie de la modération à préciser (identique au fact-check pour l'instant).
- Remplir les valeurs du prompt Fable (volumes, budget, capacité de relecture).
