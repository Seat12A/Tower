# 2026-11-11 — Réponses de Jules au plan de fact-check

## Objectif

Intégrer au plan d'implémentation du fact-check les réponses de Jules aux 18
questions ouvertes, et trancher celles qu'il a laissées ouvertes (étiquetage
politique, registre de contrôle des sources, brouillons pièges, médias, conservation
des pages).

## Réalisé

- `docs/factcheck/questions.md` : tableau des réponses et de leur effet, points encore
  à confirmer.
- Pages mises à jour :
  - `niveaux.md` : quota de 50 brouillons par jour avec sélection par doute ; liste
    des comptes suivis tenue par l'équipe ;
  - `pipeline.md` : cadence adaptative de l'ordonnanceur et délai maximal des lots ;
  - `vagues.md` ;
  - `couts.md` : plafonds de budget par défaut, palier 1 d'OpenAI ;
  - `modeles.md` : partage des données activé, palier 1 ;
  - `international.md` : langues de publication et traductions signalées ;
  - `donnees.md` : pseudonyme d'installation du jour, traductions des notes,
    étiquettes politiques restreintes, conservation des liens ;
  - `connaissances.md` : conservation des liens et lien d'archive, amorçage du
    registre de contrôle ;
  - `plateformes-medias.md` : plateforme portée partout dès le lot 2 ;
  - `biais.md`, `evaluation.md`, `feuille-de-route.md`.
- `docs/decisions.md` : section « Réponses de Jules ».

## Décisions

Voir `docs/decisions.md`. Choix faits à la place de Jules, à sa demande ou faute de
réponse :
- étiquettes politiques limitées aux partis, organisations et affiliations officielles
  d'élus et de candidats ;
- registre de contrôle amorcé depuis Wikidata ;
- brouillons pièges non activés ;
- médias au lot 8 avec empreintes dès le lot 2 ;
- liens des sources toujours conservés.

## Vérifications

- Liens internes de la documentation contrôlés par script.
- Aucun code modifié : tests, ruff et mypy non relancés.
- Non vérifié : conditions d'usage automatisé d'Internet Archive pour les liens
  d'archive ; base légale exacte de l'étiquetage des affiliations partisanes (avis
  juridique recommandé).

## Points ouverts / suite

- Confirmer que rien n'est publié sans relecture (hypothèse du plan).
- Décider de la publication de la liste des comptes suivis.
- Phase 0 : jeu de référence, comptes et clés (Anthropic, OpenAI, PISTE, INSEE,
  Google Fact Check Tools, Brave) ; puis lot 1 (fondations).

## Complément : objectivité plutôt que faux équilibre

- Demande de Jules : être objectif, pas parfaitement neutre ; ne pas être neutre sur
  le nazisme et les sujets du même ordre.
- Nouvelle page `docs/factcheck/objectivite.md` : égalité de traitement sans faux
  équilibre, registre `established_facts` à critères fermés et symétriques, effet
  dans le pipeline, lien avec la modération, garde-fous.
- Mises à jour : `CLAUDE.md` (règle 10 reformulée), `README.md`, `biais.md` (fausse
  symétrie), `evaluation.md` (cas de négation de faits établis et cas réellement
  débattus), `donnees.md`, `connaissances.md`, `pipeline.md` (lexique),
  `docs/decisions.md`.

## Complément : prompt d'implémentation de la partie 1

- `docs/factcheck/prompts/partie-1-fondations-ingestion.md` : prompt autonome pour une
  session Claude Code (Opus 5.5) qui orchestre des sous-agents Claude Sonnet 5.5 sur
  les lots 1 (fondations : ports, adaptateurs, routeur de modèles, budgets) et 2
  (ingestion générique multi-plateforme), avec exigences de RAM, de langues, de
  scalabilité et de documentation, découpage en sous-agents et vérifications.
- Fusion de la branche du plan dans `main` à la demande de Jules.
- Ajout au prompt d'une section « Économie d'usage » (forfait Claude de Jules) :
  répartition Opus et Sonnet 5.5, lecture ciblée, briefs courts, peu de sous-agents,
  sorties de commandes courtes, recherche web minimale, même niveau de qualité.
