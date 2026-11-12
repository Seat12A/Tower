# Common rules (lot 4 subagents) — read fully before coding

Repository: /home/user/Tower (12A Tower, Python 3.13 asyncio). Read `CLAUDE.md` first: its
rules override everything. Branch is already checked out: **never commit, never push, never
run `git add/commit/stash/checkout/reset`**. The orchestrator commits your files.

## Your report (mandatory, <= 10 lines)
First line: the model you are (exact name and id as you know it). Then: files touched,
tests added (count), commands run and their result, open points / deviations. No code.

## Hard rules
- Touch ONLY the files listed in your brief ("Allowed files"). Other agents work in parallel
  on other files. If you truly need another file changed, say so in your report instead.
- Code, comments, docstrings, logs, errors, tests: **English**. Docs are written by the
  orchestrator: do not edit `docs/` (except where your brief says so).
- `core/domain/**` imports the standard library only (test `test_domain_is_pure`).
- Every business write goes through a use case in `core/usecases/`: `actor.require(...)`,
  domain rules, then `events.publish(...)` in the SAME transaction (see
  `core/usecases/claims.py` for the pattern). Subscriptions are short (enqueue jobs, no
  network). Job handlers are idempotent (dedupe keys in `core/factcheck/names.py`).
- Services (`src/tower/services/*`) import only `tower.core` (no cross-service import).
- External calls only through ports/adapters; SDKs and httpx imported lazily.
- RAM: no numpy/pandas/local model; vectors are `list[float]`; heavy vector math in
  PostgreSQL (`<=>` cosine distance on `halfvec`). Any in-memory cache is bounded.
- Errors: domain errors from `core/domain/errors.py` with the most precise code of
  `core/domain/error_codes.py` (lot 4 codes: `TWR-FCK-020..038`, already defined; do NOT
  add or renumber codes — ask in your report if one is missing). Technical errors:
  `capture(ec.CODE, exc, **ctx)`. Jobs: `RetryLater`, `PermanentError`.
- Logs: `log.info("short.stable_message", key=value)`; never log in hot loops; no secrets.
- IDs: BIGINT in DB; strings in API and events (public UUIDs for exposed resources).
- Never let the author, its account, reach, political side or level reach a model input or
  influence grouping/reuse (`core/factcheck/inputs.py`, `FORBIDDEN_INPUT_KEYS`).
- Safety of reuse: when in doubt, do NOT group and do NOT reuse.
- mypy strict must pass on your files; ruff format + ruff check clean.

## Database for tests (real PostgreSQL 16 + pgvector 0.8.1, never mocked)
Use ONLY your own database: `export TOWER_DATABASE_URL=postgresql://postgres@localhost:5433/tower_test_<you>`
(`<you>` = g1…g6, given in your brief). If the server is down: `pg_ctlcluster 16 main start`.
The session fixture migrates it (downgrade base + upgrade head). Look at
`tests/conftest.py` (fixtures `db`, `settings`, `make_settings`) and existing tests
(`tests/test_factcheck_service.py`, `tests/test_gate_factcheck.py`,
`tests/test_intercom_factcheck.py`) for helpers and style; reuse their patterns.

## Commands
```bash
cd /home/user/Tower
export TOWER_DATABASE_URL=postgresql://postgres@localhost:5433/tower_test_<you>
uv run ruff format <your files> && uv run ruff check <your files>
uv run mypy                       # whole project, strict
uv run pytest -q <your test files> 2>&1 | tail -n 30
uv run pytest -q 2>&1 | tail -n 5  # once at the end: the full suite must stay green
```
Keep command outputs short (`| tail`). Do not read files you do not need. Do not rewrite
existing code beyond what your brief asks; keep diffs minimal.

## Project context
Lot 4 = fact-check V1: embeddings (E1), claim matching and groups (E3), verdict reuse,
reference facts and value bands, expiry/revalidation, processing profiles and level
overrides. The plan is in `docs/factcheck/` (pipeline.md E1/E3, niveaux.md, donnees.md,
reponses-fable.md Q7-Q9, contexte.md régimes) — in French; read only the sections your brief
cites. What lot 3 implemented: `docs/fact-check.md`. The shared data contract is
`CONTRACT.md` next to this file: read it fully.
