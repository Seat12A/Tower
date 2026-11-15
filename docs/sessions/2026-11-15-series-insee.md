# 2026-11-15 — Séries INSEE par défaut

## Objectif

Jules : remplir `TOWER_INSEE_SERIES` soi-même, « viser très large pour avoir le plus de
chiffres ». Questions de Jules traitées dans la même session (sans code) : clés et
variables d'environnement, X (Bearer Token, formulaire de cas d'usage), Brave, Google
Fact Check Tools, PISTE, nom de domaine de l'API.

## Réalisé

- `core/adapters/knowledge/insee_series.json` : 200 mots-clés vers 127 séries BDM.
  Thèmes : chômage et emploi, prix, PIB, dette et déficit, budget de l'État, commerce
  extérieur, salaires et Smic, pauvreté et inégalités, démographie, logement,
  entreprises, transports et énergie, santé, sécurité, justice, éducation.
- `core/adapters/knowledge/insee.py` : `default_series()` ; 12 observations demandées,
  valeurs `NaN` ignorées, 4 valeurs numériques gardées.
- `core/adapters/knowledge/factory.py` : liste fournie quand `TOWER_INSEE_SERIES` est vide.
- `core/config.py` : docstrings (`insee_api_key` inutilisée).
- Tests : `test_insee_skips_unpublished_values`, `test_insee_default_series` ;
  `test_insee_latest_observations` attend désormais `lastNObservations=12`.
- Documentation : `configuration.md`, `.env.example`, `fact-check.md`, `decisions.md`.

## Décisions

Voir `decisions.md`, section « Séries INSEE par défaut ».

## Vérifications

- **Appels réels** à `bdm.insee.fr` : catalogue des flux, titres des séries, et pour
  chacune des séries une lecture des dernières observations (toutes présentes). Série
  écartée : accidents du travail, sans valeur depuis 2019. Mots-clés trop vagues retirés. Lecture de bout en bout par `InseeSource.lookup` sur trois affirmations.
- `ruff`, `mypy` strict : verts. Tests INSEE sans base : verts.
- **Non vérifié** : `test_insee_latest_observations` et la suite avec base (pgvector 0.8
  absent de la session, comme pour la session précédente).

## Points ouverts / suite

- Les idbank de l'INSEE changent lors des changements de base (ex. IPC base 2025) :
  relire la liste à chaque changement de base.
- Les séries d'outre-mer et régionales ne sont pas incluses (France entière ou
  métropole seulement).
