# Légitimité des sources (statut et niveau)

**Inconnu ne veut pas dire peu fiable.** Avant ce lot, tout domaine absent du registre
humain était classé `tertiary`, au même rang qu'un blog. Résultat : le site d'Anthropic,
cité pour un fait sur Claude, ou un site de mathématiques sérieux, étaient présentés
comme peu fiables, et la règle d'indépendance (deux origines dont une primaire)
rétrogradait le verdict en « Non vérifiable ».

Ce lot sépare deux notions :

- le **niveau** (`tier`), utilisé par la règle d'indépendance : `primary`, `secondary`,
  `tertiary`, `avoid` et, nouveau, **`unrated`** (non évalué) ;
- le **statut** (`standing`), descriptif : qui publie la source.

Aucun appel de modèle supplémentaire : les trois premières étapes sont déterministes,
la dernière utilise la réponse que le juge produit déjà.

Code : `core/domain/factcheck/standing.py` (règles pures), `Atlas.rate`
(`core/adapters/knowledge/atlas.py`), `core/factcheck/standings.py` (table d'identité et
site du sujet), `core/adapters/knowledge/wikidata_sites.py` (Wikidata),
`services/atlas/identities.py` (rafraîchissement).

## Ordre de résolution

| # | Étape | Résultat | Portée |
|---|---|---|---|
| 1 | **Registre humain** (`source_registry`) | Son niveau, statut déduit du type (`institution`, `statistics`, `court`, `parliament`, `agency` → `official` ; `press` ; `encyclopedia` → `reference` ; `fact_checker` ; `academic` ; `platform` → `user_generated`) | Domaine ; a **toujours le dernier mot** (un `avoid` n'est jamais remonté) |
| 2 | **Site officiel du sujet** | `primary`, statut `first_party` | Passage (dépend de l'affirmation) |
| 3 | **Table d'identité** (`domain_identities`, Wikidata) | officiel → `primary` ; universitaire, revue → `secondary` ; presse → `unrated` (statut `press`) | Domaine ; remplace seulement l'étape 4 ou 5 |
| 4 | **Règles de suffixe fermées** | `.gov`, `.mil`, `.int`, `.gouv.fr`, `.europa.eu`, `.gc.ca`, `.admin.ch`, `.fgov.be`, `.bund.de`, `.gv.at`, `.govt.nz`, `.gub.uy`, `.gov.XX`, `.gouv.XX`, `.gob.XX`, `.go.XX` → `primary`, `official` ; `.edu`, `.edu.XX`, `.ac.XX` → `secondary`, `academic` | Domaine ; mêmes règles pour tous les pays |
| 5 | **Défaut** | `unrated`, statut inconnu | Domaine |
| 6 | **Statut donné par le juge** (sources sans statut) | officiel, universitaire, revue, référence, fact-checker → affiché `secondary` ; contenu d'utilisateur, commercial → `tertiary` ; sinon inchangé | Passage ; **affichage seulement** |

Les sources d'un adaptateur intégré (Wikipédia, Légifrance, INSEE…) gardent le niveau
déclaré par l'adaptateur (`tier_basis = adapter`).

Les plateformes d'hébergement et catalogues (`facebook.com`, `github.io`,
`blogspot.com`, `medium.com`, `academia.edu`, `oclc.org`, `books.google.com`…, liste
`SHARED_HOSTS`) ne reçoivent jamais d'identité ni de règle de suffixe, et ne sont jamais
« site officiel du sujet » : un ministère qui publie sur Facebook ne rend pas Facebook
officiel.

## Site officiel du sujet (`first_party`)

L'extraction donne déjà l'identifiant Wikidata du sujet quand il est sûr
(`signature.subject_qid`). À la collecte, le job lit dans Wikidata (deux appels
`wbgetentities` au plus, en cache) le site officiel (P856) du sujet et des entités qui
sont derrière lui : développeur (P178), fabricant (P176), éditeur (P123), opérateur
(P137), organisation mère (P749), propriétaire (P127). Les relations terminées (P582)
et dépréciées sont ignorées. Exemple : Claude → développeur Anthropic → `anthropic.com`.

- Aucune recherche par nom : sans QID, la règle ne s'applique pas (un homonyme
  donnerait un faux site officiel).
- Un passage publié sur ce site (ou un sous-domaine) est marqué
  `evidence_passages.standing = first_party` et compte comme **primaire**.
- **Garde-fou** : pour une affirmation de type `record`, `conduct` ou `accusation`, le
  site du sujet est **partie prenante** (règle fermée, comme le registre
  `controlled_by`) : il ne confirme pas seul « Anthropic respecte la loi ». Il reste
  primaire sur ses propres produits, règles et chiffres publiés.
- Les sites trouvés sont notés dans le journal de la manche
  (`investigations.queries.rounds[].subject_sites`).
- Si Wikidata ne répond pas : `TWR-ATL-017` (info), l'enquête continue sans la règle.

## Table d'identité (`domain_identities`)

Job `atlas.identity_refresh`, hebdomadaire, sur le réplica dédié `atlas` : une requête
SPARQL par classe Wikidata (instances directes `P31`, site officiel `P856`, non
dissoutes `P576`, pays `P17/P297`), lue en flux TSV (mémoire constante), écrite par lots
de 500. Classes (`IDENTITY_CLASSES`) :

| Type | Classes |
|---|---|
| `official` | agence gouvernementale, ministère, gouvernement, législature, parlement, tribunal, cour suprême, cour constitutionnelle, organisation intergouvernementale, banque centrale, organisation gouvernementale, autorité de régulation, agence spatiale, service statistique, commission électorale |
| `academic` | université (publique, privée, collégiale), institut de recherche, établissement public de recherche, académie des sciences, société savante |
| `journal` | revue académique, revue scientifique |
| `press` | journal, quotidien, journal en ligne, hebdomadaire, site d'information, agence de presse, radiodiffuseur public, radiodiffuseur, magazine d'information |

- Quand plusieurs entités revendiquent un même hôte, le type le plus haut l'emporte
  (officiel > universitaire > revue > presse).
- Les lignes non rafraîchies depuis 21 jours sont purgées (entité dissoute, site changé).
- La recherche se fait par suffixe d'hôte (le plus spécifique gagne), en base, par lot à
  chaque collecte : la table n'est **jamais chargée en mémoire**.
- **Être sur Wikidata ne prouve pas la fiabilité** : des sites de désinformation
  documentés y ont leur fiche. C'est pourquoi la presse identifiée reste `unrated`
  (statut `press`) et que le registre humain (`avoid`) l'emporte toujours.
- Échec : `TWR-ATL-016` (warning), job relancé dans une heure, les identités existantes
  restent utilisées.

## Statut donné par le juge

Pour chaque passage cité dont la source n'a pas de statut, le juge donne `standing`
dans `origins` (`official`, `academic`, `journal`, `press`, `fact_checker`, `reference`,
`organization`, `user_generated`, `commercial`, `unclear`). Le prompt lui rappelle que
`unrated` veut dire « non évalué », pas « peu fiable », et de juger l'éditeur et son
processus, jamais son orientation politique.

Ce statut est **plafonné à `secondary`** : le modèle ne peut pas fabriquer la source
primaire que la règle d'indépendance exige, donc il ne change jamais le verdict. Il sert
à l'affichage (brouillon, forum) et est enregistré dans `evidence_passages.standing`
(jamais par-dessus `first_party`). Il n'est pas renvoyé aux modèles des étapes
suivantes.

## Exposition

- `GET /v1/drafts/{id}` : chaque source citée porte `tier` (niveau effectif après les
  étapes 2 et 6), `standing` et `tier_basis` (`registry`, `adapter`, `identity`,
  `suffix`, `default`, ou `first_party`).
- Forum Discord : la liste des sources du fil affiche le statut
  (« Anthropic · subject's official site »).
- Les modèles reçoivent `tier` et `standing` de chaque source (sans le statut donné par
  le juge).

## Ce qui n'est pas couvert

- ROR (registre mondial des organismes de recherche) et DOAJ ne sont pas importés :
  leurs exports complets demandent un analyseur JSON en flux ; Wikidata couvre déjà les
  universités et instituts. À ajouter si la couverture manque.
- Les communes et collectivités ne sont pas dans la table (des centaines de classes par
  pays) ; les suffixes officiels en couvrent une partie.
- Les lignes `sources` antérieures restent `tertiary` sans `tier_basis` jusqu'à leur
  prochaine récupération (le niveau est recalculé à chaque collecte d'une URL).
- Une affirmation mathématique ou scientifique sans source primaire au sens strict
  (article de recherche, organisme de référence) reste soumise à la règle des deux
  origines dont une primaire : le site de maths est affiché « secondaire », pas
  « peu fiable », mais ne remplace pas la source primaire.
