# 2026-11-16 — Guide d'implémentation de l'extension

## Objectif
Questions de Jules sur l'extension (nom, endpoints utilisables), puis rédaction d'un
guide pour construire l'extension de A à Z.

## Réalisé
- `docs/extension.md` : contrat avec l'API (endpoints accessibles, formats, erreurs,
  quotas), architecture Manifest V3 recommandée, étapes (jeton, filtres, lecture du DOM,
  filtrage, envoi par lots, affichage des verdicts, langue, popup, Chrome Web Store,
  tests, publication), liste de contrôle et points ouverts.
- `docs/README.md` : page ajoutée au sommaire.
- `docs/configuration.md` : `TOWER_CORS_ORIGINS` corrigé (la doc disait « GET
  uniquement », le code autorise GET et POST et trois en-têtes).
- Pistes de nom données à Jules (non retenues à ce jour) : Hublot, Transpondeur,
  Boarding Pass, Cockpit, Contrail, Balise. À éviter : Copilot, tout nom contenant
  « Chrome ». Aucune vérification de marque faite.

## Décisions
- Aucune décision durable : le guide recommande (MV3, réseau dans le service worker,
  dépôt séparé, interrogation des verdicts limitée aux posts filtrés en V1) sans
  modifier Tower.

## Vérifications
- Contrat relu dans le code : `gate/routes/ingest.py`, `gate/schemas.py`
  (`PublicVerdictOut`), `gate/errors.py`, `gate/app.py` (CORS, `/openapi.json` hors
  production), `core/domain/ingestion.py` (types de médias), `core/domain/factcheck/values.py`.
- Aucun code modifié : pas de tests, ruff ni mypy lancés. Comparaison routes/`api.md`
  faite par recherche de texte (aucune route manquante, noms de paramètres différents).
- Les sélecteurs DOM de X cités (`article[data-testid="tweet"]`, `tweetText`) sont ceux
  connus à ce jour, non vérifiés sur x.com pendant la session.

## Points ouverts / suite
- Filtre de comptes par identifiant numérique alors que le DOM ne montre que le pseudo.
- Sémantique de `filters.thresholds` non définie.
- Endpoint d'index des posts vérifiés pour afficher les vérifications sans révéler la
  navigation.
- Pas de limite de débit sur `GET /v1/content/.../verdict`.
- Nom de l'extension et domaine de production de gate.
