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
- `src/tower/core/data/badge_seed.csv` : 770 comptes (institutional 538, verified 94,
  expert 68, media 65, organization 5), 128 marqués `review`. Troisième ajout : 57 comptes
  du gouvernement japonais, tous en `review` (noms affichés en japonais) ; lignes
  « À confirmer » écartées.
- Contrôle du nom resserré : le pseudo ne sert de repli que si le nom affiché n'a aucun
  mot latin (avant, un mot du nom attendu présent dans le pseudo suffisait toujours, ce
  qui rendait le contrôle sans effet pour la plupart des lignes).
- **Collecte Wikidata** (demande de Jules : « le reste des grands pays, célébrités,
  médias ») : 2 208 lignes ajoutées, tirées de Wikidata et non de mémoire. Méthode : les
  41 671 fiches qui ont un compte X avec ID et un nombre d'abonnés (P8687) d'au moins
  100 000, plus les organismes publics (ministères, agences, parlements, partis,
  organisations internationales, agences de presse) dès 10 000 abonnés ; détails par
  lots de 200 ; catégorie par les tables du dépôt (`classify_wikidata`). Personnes
  décédées et organismes dissous écartés. Un politique n'est `institutional` qu'avec un
  mandat P39 commencé en 2020 ou après et sans date de fin (Wikidata oublie souvent les
  dates de fin : Gilbert Collard sortait « en fonction »), sinon sa catégorie suivante
  ou `verified` ; un `expert` doit avoir un domaine précis et aucune profession
  politique ou artistique (Joe Biden, Zack Snyder sortaient « experts »). Quotas par
  pays (37 pays : institutions 25, médias 15, élus 12, personnalités 12, journalistes
  5, experts 5) et personnes les plus suivies au monde (personnalités 300, experts 80,
  journalistes 60). Un compte par fiche ; personnes déjà présentes sous un autre pseudo
  écartées (`@POTUS45`). Résultat : institutional 698, media 653, verified 709,
  expert 148. Limites connues : un mandat P39 peut être un poste non électif (ex. PDG
  d'entreprise publique), le pays de section suit la première nationalité (Eduardo
  Bolsonaro apparaît sous « Italy »), les domaines d'expertise restent larges.
  Total de la liste : **2 978 comptes, 2 835 avec ID Wikidata (gratuits), 143 lectures
  X au plus**.
- **Icônes des sites officiels** (choix de Jules) : `core/domain/badge_icons.py` (choix de
  l'icône dans la page, reconnaissance des formats à leurs premiers octets),
  `core/usecases/badge_logos.py` (stockage, lecture, récupération par le port
  `WebFetcher`), table `badge_logos` (migration 0018), route publique
  `GET /v1/badges/logos/{sha256}.{ext}`, codes `TWR-BDG-028` et `TWR-BDG-029`, variable
  `TOWER_PUBLIC_GATE_URL`, option `binary_types` de `HttpFetcher` (comportement par défaut
  inchangé). Colonne `website` de la liste : 1 209 sites officiels (P856) pour 1 349
  organisations et médias. Mesure réelle sur 80 sites : 64 icônes (80 %), surtout PNG,
  42 d'au moins 64 px (mesurée depuis ce conteneur par un script passant par le proxy :
  `HttpFetcher` ignore volontairement les proxys, il sortira en direct sur Railway).
- **ID figés** (demande de Jules) : colonne `x_id` ; 3 656 ID écrits (même fonction
  `lookup_wikidata` que la commande : 4 pseudos ambigus laissés à X, aucun ID en double).
  La commande ne consulte plus Wikidata que pour les lignes sans ID ; 143 lectures X au
  plus.
- **Icônes et affiliés** (demande de Jules) : colonnes `logo` et `parent` dans la liste ;
  la commande crée les affiliations (validées par l'équipe, jamais de réouverture d'une
  affiliation terminée) et pose l'icône à l'attribution. L'import Wikidata prend
  désormais l'icône (P8972, sinon P2910) au lieu du logo complet P154. Collecte :
  **seulement 18 icônes** pour 1 272 organisations et médias (la propriété est rarement
  remplie sur Wikidata) ; **194 affiliés ajoutés** (`verified`) et 6 personnes déjà
  présentes rattachées (ex. Elon Musk → Tesla, SpaceX, Neuralink ; Sam Altman →
  OpenAI ; journalistes du NYT, de Mediapart, de BFMTV). Règles et cas écartés :
  voir `decisions.md` (fondateurs, employés d'entreprises, emplois non datés). Une
  personne déjà dans la liste et affiliée passe `verified` (sauf élu en fonction) ;
  10 experts et quelques journalistes sont concernés. Total : 3 799 comptes.
- **Entreprises et ONG** (demande de Jules) : 627 lignes `organization` tirées de la même
  collecte (entreprises dès 100 000 abonnés, ONG et fondations dès 20 000, sans
  plafond) : 397 entreprises, 230 ONG et fondations (250 et 150 premières au monde, puis
  10 et 6 par pays). Types d'entreprise et d'ONG définis dans le script de collecte (la
  table du dépôt ne connaît que « company », « nonprofit »… : « public company »,
  « bank », « airline », « foundation » y manquent ; non modifiée ici). Filtres :
  fiche dont le nom se retrouve dans le pseudo (`@TfL` était sur « Legible London »),
  noms évoquant un média ou un organisme public écartés ; corrigés à la main : SBS, PBS,
  KCRW → `media`, Smithsonian → `institutional`, « Sony Centre » → « Sony ».
  Total alors : 3 605 comptes.
- **Wikidata d'abord** (demande de Jules) : `WikidataSparqlSource.accounts_by_handles`
  (P2002 + qualificatif P6552) ; un ID unique est cru sans appel à X ; hors Wikidata, X
  avec coche obligatoire (`no_check` sinon) ; `--no-wikidata`. Dry run réel contre
  Wikidata : **628 lignes gratuites sur 770**, 142 lectures X au plus (≈ 1,42 $ au lieu
  de 7,70 $). Par catégorie : institutional 426/538, verified 87/94, expert 50/68,
  media 61/65, organization 4/5.
- Précédemment : Deuxième ajout du jour :
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
- Dry run réel de la CLI : 770 lignes, 0 invalide. `tests/test_badge_seed.py` : 19 verts.
- **Appelé pour de vrai** : Wikidata (requêtes SPARQL en lecture, gratuites) pour la
  mesure de couverture, la collecte (environ 330 requêtes) et le dry run. L'API X n'a jamais été appelée.
- Aucune migration (pas de changement de schéma) ; aucun code ajouté à gate, hangar ou
  intercom (module chargé seulement par la CLI) : pas de mesure de RAM nécessaire.


- Lot des icônes (fin de session) : suite complète **2 235 verts, 2 échecs** (les mêmes
  `test_source_standing_db.py` préexistants) ; ruff, mypy strict verts ; `alembic check`
  sans différence ; migrations aller-retour 0018 → 0017 → 0018 ; RAM de gate au repos
  (lancé directement, 8 s ; « avant » dans un worktree de `1cf328a`) : 104,5 Mo avant,
  99,5 Mo après (bruit). Smoke test du vrai processus limité au démarrage de gate ; la
  route est testée par ASGI.

## Points ouverts / suite

- Coût de `--apply` complet : 143 lectures X au plus × 0,01 $ (valeur de configuration,
  tarif réel de X à vérifier) ≈ 1,4 $ sur `provider:x`.
- Icônes : environ 80 % des organisations et médias en recevront une depuis leur site au
  `--apply` (il faut `TOWER_PUBLIC_GATE_URL` sur gate) ; les autres restent sans logo
  tant que l'équipe ne l'ajoute pas. Les organisations badgées avant ne sont pas
  modifiées par la commande. Écartés : avatar X (coût, décision de Jules), logo
  Wikidata carré (essayé : 155 trouvés, environ un quart faux sur un échantillon de 30 :
  photo pour Lamborghini et la Clinton Foundation, logos de 1950 ou 2003, texte),
  favicon du site (vie privée des utilisateurs de l'extension). Piste gratuite non
  essayée : Simple Icons (icônes libres, surtout des marques tech et grand public).
- Experts hors Wikidata souvent sans coche X : ils sortiront en `no_check`, à badger à
  la main après vérification (page institutionnelle qui renvoie au compte).
- Relire la sortie : `name_mismatch` et `review` sont à vérifier à la main (`/badge`).
- Les fonctions de 2026 postérieures aux connaissances de l'assistant (ex. Premier
  ministre britannique, présidente du Pérou) viennent de la liste source ; la catégorie
  `institutional` reste juste tant que la personne a un mandat (députés, chefs de parti).
- L'import Wikidata (`POST /v1/badges/imports/wikidata`) couvre déjà une partie des
  dirigeants ; les deux ne se gênent pas (un badge `manual` l'emporte).
