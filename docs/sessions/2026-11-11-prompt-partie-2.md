# 2026-11-11 — Prompt d'implémentation de la partie 2

## Objectif

Préparer, après la livraison de la partie 1 (PR #4), le prompt de la suite pour une
nouvelle session Claude Code (Opus 5.5) qui ne connaît pas le projet.

## Réalisé

- `docs/factcheck/prompts/partie-2-radar-intercom.md` : prompt autonome. Il présente
  le projet et l'état actuel, la liste de lecture, le périmètre et les
  spécifications :
  - radar X en **service hangar** `radar`, avec planification adaptative plafonnée
    par le budget X ;
  - bot Discord `tower intercom`, client de gate, interface en français et en
    anglais, curseur SSE stocké côté gate ;
  - réservations de budget orphelines ;
  - endpoint `GET /v1/llm/usage`.
  Il précise aussi les exigences de RAM et de langues, la méthode (contrats, cinq
  sous-agents Sonnet 5.5, intégration), l'économie d'usage et le rapport attendu.
- Lien ajouté dans `docs/factcheck/README.md`.

## Décisions

- Radar proposé en service hangar plutôt qu'en processus dédié (économie d'environ
  70 Mo de RAM). La décision sera consignée par la session d'implémentation dans
  `docs/decisions.md`.

## Vérifications

- Prompt rédigé à partir du résumé de la partie 1, de la feuille de route et du code
  livré (ports, modèles, CLI, API). Aucun code modifié.

## Points ouverts / suite

- Jules doit fournir pour la partie 2 réelle : clé X, application et token Discord,
  identifiants des salons, plafond mensuel X.
- Seuil SimHash toujours à trancher sur un corpus.

## Complément : prompt du lot 3

- `docs/factcheck/prompts/partie-3-lot-3-mvp.md` : prompt autonome du MVP du
  fact-check (comptes suivis N3 et demandes manuelles N4).
  - Développement **sans clés** : doublures, fournisseur factice
    `TOWER_LLM_FAKE=1`, vrais appels à Wikipédia et Wikidata au smoke test.
  - Étages E2 et E6 à E11 ; Atlas minimal ; revue dans Discord avec boutons et
    réconciliation au démarrage ; lecture publique du verdict validé en fr, en et
    langue du post.
  - Outil `tower factcheck eval` prêt pour la session de validation avec les clés et
    le jeu de référence.
  - Prompts des modèles écrits par l'agent Opus lui-même ; six sous-agents
    Sonnet 5.5.
