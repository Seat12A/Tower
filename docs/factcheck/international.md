# Couverture mondiale

Le fact-check ne se limite ni à la France ni au français : il couvre des contenus dans
**toutes les langues** et des affirmations sur **tous les pays**. Cette page décrit
comment les données et le système le prennent en charge. Principe : rien n'est codé
pour un pays ; tout ce qui dépend d'un pays est une **donnée** (registres) ou un
**adaptateur** déclaré.

## Langue et juridiction sont deux choses différentes

| Exemple | Langue du post | Juridiction(s) de l'affirmation |
|---|---|---|
| Tweet en français sur l'élection américaine | `fr` | `US` |
| Post en espagnol sur l'inflation en France | `es` | `FR` |
| « La France a deux fois plus de X que l'Allemagne » | `fr` | `FR`, `DE` |
| Vidéo en arabe sur une loi de l'Union européenne | `ar` | `EU` |
| Rumeur sur une décision de l'OMS | toute | `INT` (organisation internationale) |

La **langue** détermine comment lire et rédiger. La **juridiction** détermine où
chercher les preuves (statistiques nationales, droit, institutions, presse du pays).

## Données

### Sur les contenus et les affirmations

| Champ | Table | Rôle |
|---|---|---|
| `lang` | `content_items` | Langue détectée (BCP 47), en E0 par heuristique puis confirmée en E2 |
| `lang_original`, `text_original` | `claims` | Affirmation canonique dans la langue du post (sert à la fidélité et à la note) |
| `text_fr` | `claims` | Traduction de travail en français, produite **dans le même appel** E2 : langue de l'équipe et langue pivot des embeddings |
| `jurisdictions` | `claims` | Tableau de codes : ISO 3166-1 (`FR`), ISO 3166-2 pour un territoire (`FR-971`, `US-CA`), codes supranationaux réservés (`EU`, `INT`) ; CHECK sur le format, index GIN |
| `primary_jurisdiction` | `claims` | Juridiction principale (routage des sources) |
| `value_numeric`, `unit`, `currency`, `value_date` | `claims` (signature) | Valeur normalisée : unités en système international, monnaie ISO 4217 avec date (une comparaison entre pays passe par une conversion datée), échelle explicite |
| `lang`, `translation_fr` | `evidence_passages` | Passage dans sa langue d'origine + traduction pour le relecteur |

**Embeddings sur `text_fr`** : tous les vecteurs sont dans le même espace, donc une
rumeur traduite en dix langues se regroupe en un seul groupe et un seul récit
([vagues.md](vagues.md)). La tâche SemEval-2025 n° 7 montre que traduire vers une
langue pivot puis vectoriser est une stratégie efficace, au même niveau que les
modèles multilingues sur les textes d'origine. Les paires interlingues font partie du
jeu de référence pour le vérifier.

**Normalisation des nombres** : séparateurs décimaux et de milliers selon la langue
(`1.000` vaut mille en français et en allemand, un en anglais), échelles (« billion »
anglais = milliard français), écritures non latines. La normalisation est faite par
E2 (sortie numérique typée) et contrôlée par du code (cohérence avec les chiffres
présents dans le texte).

**Dates** : tous les horodatages sont en UTC ; les dates relatives (« hier ») sont
résolues dans le fuseau de la juridiction principale, fourni par le registre des pays.

### Registres par pays

| Table | Contenu |
|---|---|
| `country_profiles` | Code ISO, noms, langues officielles, fuseaux, éditions Wikipédia prioritaires, niveau de couverture (A, B, C), notes |
| `jurisdiction_sources` | Pays × type de source (statistique, droit, électoral, banque centrale, santé, justice) → adaptateur ou domaine officiel, niveau de fiabilité |
| `legal_portals` | Portail juridique officiel, prise en charge ELI, modalités d'accès |
| `news_feeds` | Flux d'actualité de référence : pays, langue, éditeur, URL, actif |
| `fact_checkers` | Organisations de fact-checking : pays, langues, statut IFCN ou EFCSN, nom d'éditeur ClaimReview |
| `official_domains` | Domaines gouvernementaux et institutionnels par pays (pour restreindre la recherche web) |
| `election_calendar` | Pays, scrutin, dates : le risque thématique « élections » monte automatiquement autour d'un scrutin |

Ajouter un pays = remplir ces lignes (depuis Discord ou par une migration de données)
et, si une API nationale le justifie, écrire un adaptateur. Le pipeline ne change pas.

### Niveaux de couverture

Tous les pays ne disposent pas des mêmes sources ouvertes. Le niveau de couverture
est affiché dans le brouillon et rend le système honnête sur ce qu'il sait faire :

| Couverture | Contenu | Exemples de départ | Effet |
|---|---|---|---|
| **A** | Adaptateurs dédiés : statistique nationale, droit, actualité, fact-checkers | France, UE, Suisse, Canada, Belgique, puis pays les plus présents dans le flux | Pipeline complet |
| **B** | Sources internationales + portails officiels + recherche restreinte aux domaines officiels | Pays disposant de portails publics | Plus de recherche web, confiance calculée en conséquence |
| **C** | Sources génériques seulement (Wikidata, organisations internationales, ClaimReview, recherche) | Le reste | Abstention plus fréquente, relecteur averti |

La règle « deux sources indépendantes dont une primaire » pour « Vrai » ou « Faux »
s'applique partout : en couverture C, le système s'abstient plus souvent plutôt que de
baisser l'exigence.

## Sources mondiales (toujours disponibles)

| Source | Couverture | Accès |
|---|---|---|
| **Wikidata** | Toutes les entités, indépendant de la langue | SPARQL et REST, gratuit |
| **Wikipédia** | Éditions de la langue de la juridiction, puis anglais et français | API publiques, gratuit (voir [connaissances.md](connaissances.md)) |
| **Banque mondiale** (API Indicators v2) | ~16 000 séries, tous les pays | Gratuit, sans clé (selon des sources secondaires concordantes) |
| **FMI, OCDE, ONU** (données et indicateurs ODD) | Macro-économie, développement | API publiques, adaptateurs à valider un par un |
| **Google Fact Check Tools (ClaimReview)** | Fact-checks publiés dans le monde entier | Gratuit avec clé |
| **Réseau IFCN** | Environ 180 organisations signataires dans plus de 70 pays (estimation d'après l'enquête 2026 de l'IFCN) | Annuaire public, sert à remplir `fact_checkers` |
| **GDELT** | Presse mondiale, 65 langues traduites | Gratuit, sans clé |
| **EUR-Lex** et organisations internationales | Droit et décisions supranationaux | Gratuit |

## Pipeline : ce qui change selon la langue et le pays

| Étage | Prise en charge mondiale |
|---|---|
| E0 | Plus de filtre « français seulement » : liste des langues prises en charge en configuration (`TOWER_FC_LANGUAGES`, à créer), élargie progressivement ; une langue hors liste est comptée, pas traitée |
| E2 | Extraction dans la langue du post, sortie `text_original` + `text_fr` + juridictions + valeur normalisée en un seul appel |
| E3 | Appariement sur `text_fr` : regroupement interlingue |
| E6 | Cadrage avec le profil du pays (définitions nationales : chaque pays mesure le chômage, la pauvreté ou la délinquance à sa façon) |
| E7 | Sources choisies par juridiction dans les registres ; requêtes rédigées dans la langue des sources visées (requêtes multilingues) |
| E8 | Le juge lit les passages **dans leur langue d'origine** ; les modèles retenus lisent les grandes langues |
| E9 | Note rédigée dans la langue du post + traduction française pour le relecteur ; **les citations sont vérifiées contre le texte original, jamais contre une traduction** |
| E11 | Le relecteur valide sur la version française ; une publication dans une autre langue demande un relecteur qui la lit (voir [questions.md](questions.md)) |

## Biais propres à l'international

| Biais | Constat | Parade |
|---|---|---|
| Langues peu dotées | Les modèles sont moins fiables hors des grandes langues | Seuil de qualité mesuré **par langue** ; une langue sous le seuil part en abstention ou en relecture renforcée |
| Biais géopolitique | Des travaux de 2025-2026 montrent que les modèles appliquent des standards différents selon les pays en jeu (méthode des paires où l'on échange les noms de pays) | Paires « pays échangés » dans le jeu de référence : même structure, pays permutés, issue identique attendue |
| Sources occidentales surreprésentées | Le registre risque de mieux noter les médias de quelques pays | Niveaux fondés sur le processus éditorial, audit de la distribution par région |
| Conflit d'intérêts des sources officielles | Un gouvernement est la source primaire de ses statistiques, pas une preuve indépendante quand il défend sa propre action (conflit, répression, bilan) | Règle : une source **partie prenante** de l'affirmation ne compte pas comme source indépendante ; champ `party_to_claim` évalué au cadrage |
| Médias d'État | Indépendance éditoriale variable | Critères documentés et identiques pour tous les pays (indépendance éditoriale, correction publique), jamais le pays lui-même |
| Fuseaux et calendriers | « Hier » n'est pas le même jour partout | Résolution dans le fuseau de la juridiction |

## Niveaux et comptes suivis à l'échelle mondiale

- La liste des comptes suivis (N3) peut inclure des responsables de tous les pays
  (chefs d'État et de gouvernement, ministres, partis, institutions) selon les mêmes
  catégories de rôle public, pays par pays.
- La table des thèmes à risque s'applique partout ; le calendrier électoral la module
  par pays.
- Les budgets restent globaux par niveau ; un plafond par pays est possible
  (`budget_counters` accepte tout périmètre, par exemple `country:BR`) si un pays
  absorbe une part disproportionnée.

## Mise à l'échelle

- Un seul schéma pour tous les pays : index sur `primary_jurisdiction` et GIN sur
  `jurisdictions`, partitionnement mensuel des grosses tables ; aucune table par pays.
- Les adaptateurs nationaux sont chargés **paresseusement** : un réplica ne charge que
  le code des sources qu'il interroge réellement.
- Miroirs locaux réservés aux sources les plus consultées (mesurées dans le journal) ;
  les autres pays passent par l'accès à la demande avec cache.
