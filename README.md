# 12A Tower

Backend de **Seat 12A**, service francophone de fact-checking et de modération
centré sur X. La Tower sert l'API publique (site, extension Chromium), le bot Discord
interne et la surveillance de X, et héberge les services de fact-check et de
modération.

- Python 3.13, FastAPI, SQLAlchemy 2 (async), Alembic, PostgreSQL 16
- Une image, plusieurs processus : `tower gate` (API), `tower hangar` (worker), et à
  venir `tower radar` (surveillance X) et `tower intercom` (bot Discord)
- PostgreSQL comme seule infrastructure : données, file de jobs, bus d'événements
- Déployé sur Railway

## Démarrage rapide

```bash
uv sync
cp .env.example .env
uv run tower migrate
uv run tower gate      # http://localhost:8000/docs
uv run tower hangar
```

## Documentation

Tout est dans [`docs/`](docs/README.md) : architecture, API, dossiers, événements,
jobs, services, auth, gestion des erreurs, logs, déploiement Railway, développement,
décisions et feuille de route. Les contributeurs (et Claude) suivent
[`CLAUDE.md`](CLAUDE.md).
