# 2026-11-10 — Plan d'implémentation du fact-check

## Objectif

Transformer le plan de Fable (pipeline de fact-check E0 à E8) en plan d'implémentation
concret et détaillé pour Tower, avec des recherches en ligne pour tout vérifier.
Demandes de Jules en cours de session :

- deux fournisseurs : OpenAI pour les petits modèles très sollicités (tokens
  gratuits), Anthropic pour le raisonnement ;
- sources intégrées (Wikipédia et grandes bases ouvertes, codes juridiques de
  plusieurs pays, actualité type AFP) sans passer tout le temps par des recherches ;
- des modèles qui prennent du recul et comprennent le contexte ;
- un système de notes de contexte en plus du fact-check ;
- d'autres plateformes et le multimodal (images, vidéos) prévus dès maintenant ;
- innover ;
- coût minimal en argent et en RAM (Railway) ;
- des niveaux de traitement (un compte anonyme non viral contre un politicien ou un
  contenu viral) ;
- un système propre, compréhensible et scalable dans toutes les directions ;
- le traitement des vagues de rumeurs ;
- une couverture mondiale (tous pays, toutes langues), gérée dans les données et le
  système.

## Réalisé

- Nouveau dossier `docs/factcheck/` (15 pages) : `README.md` (vue d'ensemble, écarts
  avec le plan de Fable), `niveaux.md`, `international.md`, `pipeline.md` (étages E0 à E11, services,
  files, jobs, topics, ports, RAM), `vagues.md`, `modeles.md`, `connaissances.md`
  (Atlas), `contexte.md`, `biais.md`, `plateformes-medias.md`, `donnees.md`,
  `couts.md`, `evaluation.md`, `feuille-de-route.md`, `questions.md`, `recherche.md`
  (sources consultées et dates).
- Mises à jour : `docs/README.md` (sommaire), `docs/roadmap.md` (renvoi vers le
  plan), `docs/services.md` (services prévus : `factcheck`, `investigate`, `atlas`,
  `media`), `docs/decisions.md` (décisions proposées et options écartées).
- Aucun code modifié.

## Décisions

Proposées, à valider par Jules (liste complète dans `docs/decisions.md`) :

- routage des modèles par rôle, table versionnée ; OpenAI pour les petits rôles et le
  contradicteur, Anthropic pour le cadrage, le jugement, la rédaction et l'audit ;
- embeddings par API plutôt qu'un modèle local : sur Railway (~10 $/Go/mois), un
  modèle local coûte 20 à 25 $/mois de RAM contre moins de 1 $/mois de tokens ;
- niveaux N0 à N4 ; rôle public autorisé, bord interdit ; juge aveugle ;
- Atlas : connaissance structurée avant la recherche web ;
- étage de cadrage (prendre du recul) ; deux axes de sortie (véracité, contexte) ;
- récits et mode vague ;
- micro-lots d'extraction, cache de contenu des appels, réservation de budget
  atomique, confiance calculée et calibrée ;
- modèle de contenu multi-plateforme, médias prévus dans le modèle de données ;
- couverture mondiale : langue et juridiction sur chaque affirmation, français comme
  langue pivot des embeddings (regroupement interlingue), registres de sources par
  pays, niveaux de couverture A/B/C.

## Vérifications

- Recherches en ligne effectuées et sourcées dans `docs/factcheck/recherche.md`.
  Vérifié sur source officielle : tarifs OpenAI (page de tarifs, fiche
  `gpt-6-luna`), limites des API Wikimedia, LEGI et API Légifrance, EUR-Lex, Fedlex,
  lois du Canada, INSEE, Eurostat, Google Fact Check Tools, GDELT, Bluesky Jetstream,
  Threads, AI Act article 50 (FAQ de la Commission), tarifs Anthropic (référence
  embarquée du 2026-11-03).
- **Non vérifié sur source officielle** : conditions exactes du programme de tokens
  gratuits OpenAI (page d'aide en erreur 403 ; sources secondaires datées d'août
  2026), éligibilité de la famille GPT-6 à ce programme, tarifs de l'API X (sources
  secondaires), accès programmatique à Justel, Legilux, LégisQuébec et EUvsDisinfo,
  quotas PISTE, INSEE et Google Fact Check Tools, version de pgvector sur Railway,
  tarif exact de la transcription.
- Les estimations de coûts reposent sur le scénario supposé du plan de Fable
  (20 000 posts par jour) et des hypothèses de tokens, pas sur des mesures.
- Liens internes de la documentation vérifiés par script (voir le commit).
- Tests, ruff et mypy non relancés : aucun fichier de code modifié.

## Points ouverts / suite

- Réponses de Jules aux questions de `docs/factcheck/questions.md`, en particulier :
  capacité de relecture, validation juridique du partage de données avec OpenAI,
  palier d'usage et modèles éligibles, étiquetage politique (RGPD art. 9).
- Questions d'architecture à soumettre à Fable (même page) : neutralité du niveau N3,
  taille des micro-lots, contradicteur inter-fournisseurs, règles de fusion des
  récits, identifiants multi-plateformes, détection de vague sans historique,
  échantillonnage sous charge.
- Étape suivante proposée : phase 0 (jeu de référence, comptes et clés), et en
  parallèle le lot 1 (fondations : ports, routeur de modèles, budgets).

## Complément : questions pour Fable

- `docs/factcheck/questions-fable.md` : brief autonome (contexte commun, puis onze
  questions avec ce qui est prévu, les options, le doute et le format de réponse
  attendu), à transmettre à Fable. `questions.md` y renvoie.
