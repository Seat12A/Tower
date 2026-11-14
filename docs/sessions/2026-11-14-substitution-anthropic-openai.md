# 2026-11-14 — Substitution temporaire des modèles Anthropic par OpenAI

## Objectif

Jules : « temporairement, passer tous les modèles d'Anthropic sur OpenAI », pour le coût.

## Réalisé

- `core/config.py` : variable `TOWER_LLM_MODEL_SUBSTITUTIONS` (JSON), avec une valeur
  par défaut temporaire qui remplace les quatre modèles Anthropic.
- `core/llm/router.py` : `LLMRouter(model_substitutions=…)` remplace les modèles de la
  route au chargement (ordre conservé, doublons retirés, cible inconnue → `TWR-LLM-009`).
  Routes en base inchangées.
- `core/factcheck/runtime.py` : passe la variable au routeur.
- Tests : trois tests dans `tests/test_llm_router.py`.
- Documentation : `llm.md` (étape 1 du déroulé, nouvelle section), `configuration.md`,
  `.env.example`, `decisions.md`.

## Décisions

- Haiku → `gpt-6-luna` ; Sonnet, Opus, Fable → `gpt-6.1-terra` (pas de modèle à groupe
  gratuit : les routes concernées sont sans partage). Détails dans `decisions.md`.
- Retour à Anthropic : `TOWER_LLM_MODEL_SUBSTITUTIONS={}`.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- `test_default_substitutions_cover_every_seeded_anthropic_model` (sans base) : vert.
- **Non vérifié** : les tests du routeur avec base. La base locale n'a que pgvector 0.6
  (paquet Debian) ; la compilation de pgvector 0.8 depuis les sources a été refusée par
  le mode automatique de la session. Suite complète, `alembic check` et smoke test non
  lancés (aucune migration dans ce changement).
- Aucun appel réel à un modèle.

## Points ouverts / suite

- Lancer `uv run pytest -q tests/test_llm_router.py` sur une base avec pgvector ≥ 0.8.
- Configurer `TOWER_OPENAI_API_KEY` (clé sans partage) : sans elle, `frame`, `judge`,
  `writer` et `auditor` n'ont plus aucun candidat (`TWR-LLM-008`).
- Juge et contradicteur chez le même fournisseur pendant la substitution.
