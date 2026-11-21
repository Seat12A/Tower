# 2026-11-16 — Guide d'implémentation de l'extension

## Objectif
Questions de Jules sur l'extension (nom, endpoints utilisables), puis rédaction d'un
guide pour construire l'extension de A à Z.

## Réalisé
- `docs/extension.md` : contrat avec l'API (endpoints accessibles, formats, erreurs,
  quotas), architecture Manifest V3 recommandée, étapes (jeton, filtres, lecture du DOM,
  filtrage, envoi par lots, affichage des verdicts, langue, popup, Chrome Web Store,
  tests, publication), liste de contrôle et points ouverts.
- Deuxième passe, après fusion de `main` (badges, légitimité des sources) : section
  « Le projet en bref » (Seat 12A, Tower, processus, chemin d'un post), section
  « Internationalisation (obligatoire) », étape « Badges Seat 12A » (catégories, lookup
  en lot, masquage des coches X, indices « government », demandes et signalements),
  endpoints de badges dans le tableau du contrat, liste de contrôle et points ouverts
  mis à jour.
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
  production), `core/domain/ingestion.py` (types de médias), `core/domain/factcheck/values.py`,
  `gate/routes/badges.py` (catégories, corps des demandes, signalements et indices).
- Aucun code modifié : pas de tests, ruff ni mypy lancés. Comparaison routes/`api.md`
  faite par recherche de texte (aucune route manquante, noms de paramètres différents).
- Les sélecteurs DOM de X cités (`article[data-testid="tweet"]`, `tweetText`) sont ceux
  connus à ce jour, non vérifiés sur x.com pendant la session.

## Points ouverts / suite
- **Bloquant pour les badges** : le lookup, les indices « government » et le filtre de
  comptes exigent l'ID numérique de l'auteur, que le DOM de X n'expose pas de façon
  stable. Source côté client à étudier, ou correspondance pseudo → ID servie par Tower
  pour les comptes connus.
- Sémantique de `filters.thresholds` non définie.
- Endpoint d'index des posts vérifiés pour afficher les vérifications sans révéler la
  navigation.
- Pas de limite de débit sur `GET /v1/content/.../verdict`.
- Nom de l'extension et domaine de production de gate.
