# 2026-11-16 — Légitimité des sources inconnues

## Objectif

Jules : une source absente du registre était notée comme peu fiable (site d'Anthropic
cité pour un fait sur Claude, site de maths sérieux). Il faut juger la légitimité d'une
source qu'on ne connaît pas à l'avance, sans consommer trop de ressources. Plan validé par
Jules : niveau « non évalué », règles fermées, site officiel du sujet via Wikidata, table
d'identité des domaines depuis Wikidata, statut donné par le juge, le tout combiné.

## Réalisé

- **Cause** : `Atlas.tier_of` classait tout domaine inconnu `tertiary` ; le juge le
  plaçait sous la presse et la règle d'indépendance (deux origines dont une primaire)
  rétrogradait le verdict (avertissement `downgraded`).
- **Domaine pur** `core/domain/factcheck/standing.py` : niveau `unrated`, statuts
  (`official`, `academic`, `journal`, `press`, `first_party`…), règles de suffixe fermées,
  plateformes exclues, `effective_tier` (site du sujet → primaire, statut du juge plafonné
  à secondaire), règle partie prenante du site du sujet.
- **Atlas** : `Atlas.rate` (registre > suffixes > `unrated`), `AtlasDocument.standing` et
  `tier_basis`.
- **Site officiel du sujet** (`core/adapters/knowledge/wikidata_sites.py`) : QID du sujet →
  P856 du sujet et de son développeur, fabricant, éditeur, opérateur, organisation mère,
  propriétaire (deux appels `wbgetentities` en cache) ; passages marqués `first_party` à la
  collecte (`services/investigate/jobs.py`), sites notés dans le journal de la manche.
- **Table `domain_identities`** + job `atlas.identity_refresh` (`services/atlas/identities.py`) :
  34 classes Wikidata, SPARQL lu en flux TSV, upsert par lots de 500, type le plus haut
  gagnant, purge à 21 jours ; consultée en base par lot à chaque collecte
  (`core/factcheck/standings.py`), jamais chargée en mémoire.
- **Juge** : `PassageOrigin.standing` (facultatif, anciennes réponses toujours lisibles),
  prompts `judge` et `second_opinion` (« unrated ≠ peu fiable », `first_party`) ; statut
  enregistré dans `evidence_passages.standing`, affichage seulement.
- **Exposition** : brouillon (`tier` effectif, `standing`, `tier_basis`), forum Discord
  (statut à côté de chaque source).
- Migration **0013** (expand) : `unrated` dans `ck_sources_tier` (`NOT VALID` puis
  `VALIDATE`), colonnes `sources.standing`, `sources.tier_basis`,
  `evidence_passages.standing`, table `domain_identities`.
- Codes `TWR-ATL-016` (rafraîchissement échoué), `TWR-ATL-017` (sites du sujet illisibles).
- Docs : nouvelle page `factcheck/sources.md` ; `database.md`, `api.md`, `services.md`,
  `intercom.md`, `factcheck/{README,connaissances,donnees,pipeline}.md`, `decisions.md`,
  `errors.md` régénéré.
- Aucun sous-agent.

## Décisions

Voir `decisions.md`, section « Légitimité des sources inconnues ». En bref : registre
humain toujours prioritaire ; site du sujet par QID uniquement (jamais par nom ni par le
modèle) ; presse identifiée = `unrated` (Wikidata dit qui publie, pas si c'est fiable) ;
statut du juge plafonné à secondaire (il ne peut pas changer le verdict) ; ROR, DOAJ et
collectivités non importés dans ce lot.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- Tests sans base : **1 303 verts**, aucun échec (32 nouveaux dans `test_source_standing.py`) ; un test
  existant adapté (`test_tier_of_suffix_most_specific_wins` : inconnu → `unrated`).
- **Non exécutés** : les 805 tests avec base (erreur de mise en place : pgvector), dont les 5 nouveaux de
  `test_source_standing_db.py` (job d'identités, purge, règle `first_party` à travers les
  vrais jobs, panne de Wikidata). pgvector 0.6 (dépôt Ubuntu) a pu être installé, mais le
  projet exige ≥ 0.8 et l'ajout du dépôt apt.postgresql.org a été refusé par le mode
  automatique. Donc pas de suite complète, ni `alembic check`, ni aller-retour complet des
  migrations.
- **Vérifié à la main sur une base PostgreSQL de travail** (tables minimales à l'ancienne
  forme) : migration 0013 montée, descendue (les `unrated` repassent `tertiary`) et
  remontée ; contrainte `standing` qui refuse une valeur inconnue ; upsert des identités
  (doublon dans un lot, type plus haut conservé) ; recherche par suffixe ;
  `apply_identities` ; upsert des `sources` qui recalcule le niveau d'une ligne existante.
- **Appels réels** : import complet des 34 classes depuis `query.wikidata.org` dans la base
  de travail : **87 825 hôtes** (officiel 12 863, universitaire 23 844, revues 38 068,
  presse 13 050), 12 Mo, 98 s. Contrôles : `insee.fr`, `who.int` officiels, `mit.edu`,
  `sorbonne-universite.fr` universitaires, `ams.org`, `nature.com` revues, `lemonde.fr`,
  `afp.com`, `reuters.com` presse. Chemin Claude (Q118876059) → Anthropic (Q116758847) →
  `https://www.anthropic.com/` vérifié par SPARQL ; l'appel `wbgetentities` lui-même
  renvoie **429** depuis ce conteneur (limitation de l'IP du proxy), il est testé avec un
  faux transport.
- **RAM** (processus lancés directement, mesure à 8 s, « avant » = worktree de `ce7f1dd`,
  base de travail sans schéma complet) : gate 96,8 / 95,7 Mo avant, 95,9 / 95,9 Mo après ;
  hangar (`atlas,investigate`) 87,8 / 86,9 Mo avant, 87,1 / 87,1 Mo après : écart nul au
  repos. Valeur après traitement non mesurée (pas de schéma complet).
- Aucun appel de modèle.

## Points ouverts / suite

- Lancer la suite avec base (pgvector ≥ 0.8) : en local, ou autoriser le dépôt PGDG ici.
- Au déploiement : le réplica `atlas` remplit `domain_identities` dans la semaine (ou
  enfiler le job à la main) ; d'ici là, suffixes et site du sujet fonctionnent déjà.
- Le site et l'extension peuvent afficher `standing` et ne plus présenter `unrated` comme
  un avertissement.
- Affirmations mathématiques ou scientifiques : la règle « deux origines dont une
  primaire » s'applique toujours ; un site de maths est affiché secondaire, il ne remplace
  pas une source primaire. À rediscuter si Jules veut une règle propre aux faits établis.
- Sources à ajouter si la couverture manque : ROR, DOAJ, collectivités.
