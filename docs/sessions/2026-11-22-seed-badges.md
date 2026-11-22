# 2026-11-22 — Liste amorce de badges (`tower badges seed`)

## Objectif

Jules veut enrichir la base des comptes certifiés à partir de plusieurs listes
(dirigeants mondiaux, responsables politiques FR/US/UE/monde, institutions des États
français, américain, britannique, allemand et espagnol, organisations internationales,
médias, personnalités, experts), avec une commande à lancer dans la console Railway, et
en respectant la catégorie : `institutional` seulement pour les personnes en fonction,
les autres en `verified`.

## Réalisé

- `src/tower/core/badge_seed.py` : validation du CSV, contrôle du nom, `seed_one`
  (résolution par `resolve_x_handle`, attribution par `grant_badge`), `run_seed`.
- `src/tower/core/data/badge_seed.csv` : 713 comptes (institutional 481, verified 94,
  expert 68, media 65, organization 5), 71 marqués `review`. Deuxième ajout du jour :
  51 comptes de l'État français (services, agences, autorités indépendantes,
  établissements publics, préfectures, diplomatie) ; doublons et « À confirmer » écartés ;
  France Universités et UNSS en `organization` (associations) ; `@Ccomptes` et
  `@Courdescomptes` tous deux en `review` (seul celui à coche grise sera badgé).
- CLI : `tower badges seed [--apply] [--category …] [--limit N] [--include-review]
  [--file …]`.
- Tests : `tests/test_badge_seed.py` (14).
- Docs : `badges.md` (section « Liste amorce »), `development.md`, `decisions.md`.

## Tri des listes fournies

Listes non authentifiées (générées ou tirées de worldleaders.website) : la commande ne
fait confiance qu'à l'API X et au contrôle du nom.

- **Écartés** : pseudos de plus de 15 caractères, donc impossibles sur X
  (`@FukuyamaInstitute`, `@KimLaneScheppele`, `@JamesK_Galbraith`, `@SIG_Communication`,
  `@AgenciaTributaria`) ; personnes sans compte (Xi Jinping, Poutine, MBS, Pezeshkian,
  Merkel, Rosanvallon) ; personnes décédées (Daniel Kahneman, Bruno Latour, Joseph Nye) ;
  comptes connus comme non officiels (`@ThomasSowell`, `@vaclavsmil`) ;
  `@Chris_WhiteUK` (pseudo britannique pour un maire chilien : incohérent).
- **Corrigés** : `@drprevost` → `@Pontifex` (compte officiel du pape ; l'ancien compte
  date d'avant le pontificat) ; « Cour des comptes » → `@Ccomptes` (lien de la liste) ;
  `@gabriezucman` → `@gabriel_zucman` (le contrôle du nom protège si c'est faux).
- **Catégories** : anciens dirigeants en `verified` (Obama, Biden, Clinton, Harris,
  Sarkozy, Royal, Habeck, Boluarte, Petro et Boric dont le mandat a pris fin en 2026),
  Mélenchon en `verified` (aucun mandat) ; direction de parti en `institutional`
  (Retailleau, Tondelier, Roussel, Villepin, Varoufakis…) ; Fareed Zakaria en `media` ;
  McKibben, Doctorow, Snowden en `verified` ; CICR, FICR et ISO en `organization` (pas
  des organisations intergouvernementales).
- **`review`** : comptes peu connus ou douteux (`@HassanalBolkia4`, `@drmohamedirfaa1`,
  `@cis_mfa`, `@DOGE`, `@RapidResponse47`, plusieurs maires et gouverneurs…) et experts
  dont le compte n'est pas confirmé. Attribués seulement avec la coche grise de X.

## Décisions

Voir `decisions.md`, « Liste amorce de badges ». En bref : commande dans le paquet plutôt
qu'un script collé ; dry run par défaut ; jamais de modification d'un compte déjà
badgé, bloqué ou retiré ; contrôle du nom ; `review` réservé aux comptes `government`.

## Vérifications

- `ruff format --check`, `ruff check`, `mypy` strict : verts.
- `tests/test_badge_seed.py` : 14 verts (PostgreSQL 16 réel + pgvector 0.8.7 extrait à
  la main dans le conteneur).
- Suite complète : 2218 verts, 2 échecs dans `tests/test_source_standing_db.py`
  (`test_identity_refresh_errors`, `test_subject_sites_failure_does_not_stop_the_investigation`),
  fichier déjà noté en échec préexistant le 2026-11-21, sans lien avec les badges. Non corrigés.
- CSV présent dans la roue construite (`uv build`).
- Dry run réel de la CLI : 713 lignes, 0 invalide.
- Aucune migration (pas de changement de schéma) ; aucun code ajouté à gate, hangar ou
  intercom (module chargé seulement par la CLI) : pas de mesure de RAM nécessaire.
- **Rien d'externe appelé** : ni l'API X, ni Wikidata. Les pseudos n'ont donc pas été
  vérifiés en direct : c'est le rôle de `--apply`.

## Points ouverts / suite

- Coût de `--apply` complet : environ 713 lectures × 0,01 $ ≈ 7,1 $ sur `provider:x`
  (plafond par défaut 20 $/mois, partagé avec le radar). Lancer par catégorie si besoin.
- Relire la sortie : `name_mismatch` et `review` sont à vérifier à la main (`/badge`).
- Les fonctions de 2026 postérieures aux connaissances de l'assistant (ex. Premier
  ministre britannique, présidente du Pérou) viennent de la liste source ; la catégorie
  `institutional` reste juste tant que la personne a un mandat (députés, chefs de parti).
- L'import Wikidata (`POST /v1/badges/imports/wikidata`) couvre déjà une partie des
  dirigeants ; les deux ne se gênent pas (un badge `manual` l'emporte).
