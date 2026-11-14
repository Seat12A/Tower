# Développement local

## Prérequis

- Python 3.13, [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 avec l'extension **pgvector ≥ 0.8** (lot 4 ; local ou Docker)

```bash
uv sync
```

```bash
cp .env.example .env    # puis ajuster TOWER_DATABASE_URL
```

```bash
docker run -d --name tower-pg -e POSTGRES_HOST_AUTH_METHOD=trust -p 5432:5432 pgvector/pgvector:0.8.1-pg16
```

Sans Docker (Debian/Ubuntu) : le paquet `postgresql-16-pgvector` des dépôts Debian peut
être trop ancien (0.6 : pas de `halfvec`). Compiler alors depuis les sources :

```bash
apt-get install -y postgresql-server-dev-16 build-essential
```

```bash
git clone --depth 1 --branch v0.8.1 https://github.com/pgvector/pgvector.git /tmp/pgvector && make -C /tmp/pgvector && sudo make -C /tmp/pgvector install
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
| `tower atlas seed` | Amorce les flux d'actualité de référence et les registres par pays (idempotent, n'écrase pas une ligne modifiée par l'équipe) |
| `tower atlas legi-sync` | Synchronise le miroir LEGI maintenant (dump global la première fois, puis différences) et affiche taille, durée et RSS de pointe |
| `tower factcheck calibrate [--dry-run]` | Recalibre la confiance sur les revues (candidat inactif ; refusé sous 200 revues, modèle complet à partir de 100 refus) |
| `tower factcheck activate-calibration --model ID --reason …` | Active un modèle de confiance, seulement si son ECE hors échantillon bat la calibration courante |

## Test de charge

`scripts/loadtest_wave.py` (script autonome, hors paquet, non collecté par pytest) injecte
une vague synthétique contre des processus en marche : variantes d'une même rumeur
française (copies, quasi-copies, chiffres nouveaux, traductions avec le marqueur
`[fr: …]`), réparties sur de nombreux faux auteurs et installations, plus du bruit de fond
sans rapport, via `POST /v1/ingest/observations` (lots de 50, un jeton d'installation
local par tranche de 400 posts pour respecter le quota par installation).

Critère du lot 5 : 100 000 posts en 24 h sur un récit, sans retard des files N3, sans
dépassement de budget, premier brouillon en moins de 2 h, flux de masse sous 0,00001 $ par
post. Le script mesure : débit d'ingestion, délai `content.ingested` → `draft.ready` des
posts du compte surveillé (p50, p95), file d'attente par file (SQL sur `jobs`, pic),
transitions d'état des récits et heure du premier brouillon de vague, part `sampled_out`,
coût modèle et embeddings par post (`llm_calls`), compteurs de budget au-delà du plafond.

Lancement local (outils du smoke test de la session précédente dans
`docs/factcheck/prompts/lot-5-briefs/` : `start.sh`, `env.sh`, `fake_x.py`) :

```bash
source docs/factcheck/prompts/lot-5-briefs/env.sh   # TOWER_LLM_FAKE=1, gate sur 0.0.0.0, services du fact-check
uv run tower migrate && uv run tower llm seed
uv run tower gate &  uv run tower hangar &          # hangar avec TOWER_SERVICES=...,factcheck,investigate
python docs/factcheck/prompts/lot-5-briefs/fake_x.py posts.json 8799 &   # faux serveur X (radar)
uv run python scripts/loadtest_wave.py --gate-url http://127.0.0.1:8765 \
    --database-url "$TOWER_DATABASE_URL" --posts 5000 --minutes 10 \
    --narrative-variants 30 --watched-post-every 60 --watched-post-file posts.json
```

Posts du compte surveillé (N3, immédiat) : soit via le faux serveur X (`--watched-post-file`,
le radar les récupère), soit en ingestion directe avec un jeton de service portant
`content:ingest` (`--watched-service-token`, source `radar`). Le compte doit être dans la
liste de suivi (`--watched-handle`, `--watched-author-id`). `--drain-seconds` laisse le
temps aux files de se vider avant les mesures. `python scripts/loadtest_wave.py --help`
liste tous les arguments.

## Nouvelle table

1. Modèle dans `core/models/`, importé dans `core/models/__init__.py`.
2. `uv run alembic revision --autogenerate -m "…"`, relire et nettoyer.
3. `uv run tower migrate`, puis `uv run alembic check`.
4. Mettre à jour [database.md](database.md).
