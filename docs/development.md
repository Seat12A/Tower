# Développement local

## Prérequis

- Python 3.13, [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 (local ou Docker)

```bash
uv sync
```

```bash
cp .env.example .env    # puis ajuster TOWER_DATABASE_URL
```

```bash
docker run -d --name tower-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 5432:5432 postgres:16
```

```bash
createdb -h localhost -U postgres tower && createdb -h localhost -U postgres tower_test
```

## Lancer

```bash
uv run tower migrate
```

```bash
uv run tower gate       # http://localhost:8000/docs
```

```bash
uv run tower hangar
```

## Vérifications (identiques à la CI)

```bash
uv run ruff format src tests && uv run ruff check src tests
```

```bash
uv run mypy
```

```bash
TOWER_DATABASE_URL=postgresql://postgres@localhost:5432/tower_test uv run pytest -q
```

Les tests utilisent une **vraie base PostgreSQL** (`tower_test`, vidée entre chaque
test) : l'outbox, la file et LISTEN/NOTIFY ne se simulent pas. Le flux SSE est testé
contre un vrai uvicorn (httpx `ASGITransport` ne diffuse pas une réponse infinie).

## Commandes CLI

| Commande | Effet |
|---|---|
| `tower gate` | API |
| `tower hangar` | Worker |
| `tower migrate [rev]` / `--downgrade rev` | Migrations |
| `tower create-service-token <nom> --scopes …` | Nouveau client de service |
| `tower staff <discord_id> --roles … [--name] [--disable]` | Membre de l'équipe |
| `tower errors list [--all]` | Erreurs stockées |
| `tower errors resolve <empreinte> [--note]` | Résoudre une erreur |
| `tower errors catalog --markdown` | Catalogue (régénère `docs/errors.md`) |

## Nouvelle table

1. Modèle dans `core/models/`, importé dans `core/models/__init__.py`.
2. `uv run alembic revision --autogenerate -m "…"`, relire et nettoyer.
3. `uv run tower migrate`, puis `uv run alembic check`.
4. Mettre à jour [database.md](database.md).
