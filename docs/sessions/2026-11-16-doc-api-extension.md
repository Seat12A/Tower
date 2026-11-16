# 2026-11-16 — Documentation de l'API de l'extension

## Objectif
Une référence unique et exacte des routes appelées par l'extension Chromium : corps
envoyés, réponses, en-têtes, erreurs.

## Réalisé
- `docs/extension-api.md` : 8 routes (config, observations, verdict public, badges :
  catégories, lookup, détail, signalement, indices gouvernementaux X), jeton
  d'installation, CORS, format d'erreur, limites et quotas.
- Lien ajouté dans `docs/README.md`.

## Décisions
- Aucune.

## Vérifications
- Contenu vérifié par lecture du code (`gate/routes/ingest.py`, `badges.py`,
  `factcheck.py`, `gate/schemas.py`, `core/domain/{privacy,ingestion,badges}.py`,
  `factcheck/values.py`, `gate/app.py`). Documentation seulement : aucun test lancé,
  aucun appel réel à gate.

## Points ouverts / suite
- Les exemples de valeurs (`metrics`, `thresholds`) sont indicatifs : clés libres côté
  serveur ; à figer avec l'extension si besoin.
