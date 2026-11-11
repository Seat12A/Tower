# Atlas : sources de connaissance intégrées

Demande de Jules : les modules doivent avoir accès à Wikipédia et aux grandes bases
ouvertes, aux codes juridiques de plusieurs pays et à l'actualité **sans passer tout le
temps par des recherches**. Atlas est la couche qui répond à ce besoin : un ensemble
d'adaptateurs de sources derrière un port commun, interrogés dans un ordre fixe, du
moins cher et plus fiable au plus cher et moins fiable.

Une étude de 2025 sur 15 modèles et plus de 6 000 affirmations PolitiFact conclut que
la recherche web générale n'apporte qu'un gain modéré, alors qu'un contexte **curé**
(des résumés de fact-checks fournis au modèle) augmente le F1 macro de 233 % en moyenne
(voir [recherche.md](recherche.md)). Atlas applique ce constat : la recherche web
arrive en dernier.

## Ordre de résolution

```
1. Faits établis et fiches de référence  gratuit, instantané, déjà vérifié
2. Fact-checks existants               ClaimReview (Google Fact Check Tools), nos verdicts publiés
3. Sources structurées par type        statistiques, entités, droit
4. Encyclopédies                       Wikipédia (comme carte vers les sources primaires)
5. Actualité de référence              flux d'éditeurs, GDELT, AFP (si contrat)
6. Recherche web générale              payante, bornée par le niveau, domaines filtrés
```

L'étage E5 (vérification légère) s'arrête généralement aux niveaux 1 à 3. L'étage E7
(collecte) descend jusqu'au niveau autorisé par le niveau de traitement
([niveaux.md](niveaux.md)). Chaque niveau consulté est journalisé : on mesure combien
d'affirmations sont tranchées sans recherche payante.

## Contrat d'une source

```python
class KnowledgeSource(Protocol):
    name: str                          # "wikidata", "legi_fr", "insee_bdm", ...
    capabilities: SourceCapabilities   # claim types, jurisdictions, languages
    reliability_tier: Tier             # primary / secondary / tertiary
    cost_per_call_usd_micros: int
    license: str                       # "Licence Ouverte 2.0", "CC BY-SA 4.0", ...

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]: ...
```

`Evidence` porte le texte du passage, l'URL canonique, la date de publication ou de
version, l'empreinte du contenu, l'identifiant stable dans la source (QID Wikidata,
identifiant LEGI d'un article, code de série INSEE, numéro de révision Wikipédia),
la licence.

Le **SourceRouter** choisit les adaptateurs à partir de la signature de l'affirmation
(`claim_type`, `jurisdiction`, thème) : une statistique française va vers l'INSEE et
Eurostat, une affirmation juridique suisse vers Fedlex. Ajouter un pays ou une source,
c'est ajouter un adaptateur et une ligne dans `knowledge_sources` ; le reste du
pipeline ne change pas.

La couverture est **mondiale** : les sources de chaque pays sont décrites par des
registres en base (statistique, droit, actualité, fact-checkers, domaines officiels),
complétés par des sources internationales toujours disponibles (Wikidata, Banque
mondiale, FMI, OCDE, ONU, ClaimReview, GDELT). Les sources françaises détaillées
ci-dessous sont les premiers adaptateurs dédiés, pas une limite du système. Voir
[international.md](international.md).

## 1. Fiches de référence (cache de faits)

Reprise du plan de Fable : une fiche stocke un fait vérifié (indicateur, valeur,
unité, période, territoire, source primaire, date de publication, date attendue de la
prochaine publication). Quelques centaines d'indicateurs couvrent l'essentiel des
débats. Une variante chiffrée se tranche par comparaison, sans modèle coûteux.

Ajout : les fiches **statistiques** sont alimentées automatiquement depuis les API
statistiques (niveau 3) pour une liste d'indicateurs suivis (chômage, inflation, dette,
PIB, population, immigration, délinquance enregistrée…), avec revalidation à la date
de prochaine publication. Les fiches créées par l'enquête sont validées par un
relecteur avant de servir.

## 2. Fact-checks existants

| Source | Accès | Usage |
|---|---|---|
| **Google Fact Check Tools API** (`claims:search`) | Gratuite avec clé d'API Google ; quotas visibles dans la console ; version `v1alpha1` | Retrouver les fact-checks publiés au format ClaimReview par les rédactions (AFP Factuel, Les Décodeurs, Libération CheckNews, etc. quand elles balisent leurs articles) |
| **Nos verdicts publiés** | Base interne | Réutilisation directe selon les règles de cache du niveau |
| **EUvsDisinfo** | Base publique des cas de désinformation pro-Kremlin, mise à jour hebdomadaire (accès programmatique à confirmer) | Récits de désinformation étatique connus |

Méthode d'appariement inspirée de la tâche SemEval-2025 n° 7 (retrouver des
fact-checks multilingues) : recherche hybride (embeddings + mots-clés), puis
re-classement. On ne copie pas le fact-check : on stocke le lien, le titre, l'issue
déclarée, la date, et le passage cité au besoin.

## 3. Sources structurées par type

### Entités, dates, fonctions : Wikidata

- Requêtes SPARQL (service de requêtes) et API REST de Wikidata, gratuites.
- Depuis mai 2025, le graphe est scindé en deux (« main » et « scholarly ») ; les
  requêtes qui couvrent les deux passent par une fédération `SERVICE`.
- Usage : qui occupe quelle fonction et depuis quand, dates de naissance, populations,
  appartenances, avec **références et qualificatifs** (dates de validité). Une valeur
  sans référence est traitée comme tertiaire.

### Statistiques

| Source | Accès | Couverture |
|---|---|---|
| **INSEE** — API BDM (SDMX) et catalogue api.insee.fr | Gratuite, inscription au portail | Séries chronologiques françaises, calendrier de publication (utile pour la péremption) |
| **Eurostat** — API SDMX 3.0 et API Statistics (JSON-stat 2.0) | Gratuite | Comparaisons européennes |
| **Banque mondiale, OCDE, FMI** | API publiques (à confirmer adaptateur par adaptateur) | Comparaisons internationales |
| **data.gouv.fr** | API publique | Jeux de données publics français |

Les adaptateurs statistiques renvoient une **valeur structurée** (indicateur, valeur,
unité, période, territoire, date de publication), pas du texte : la comparaison avec
l'affirmation est déterministe, le modèle ne sert qu'à vérifier que c'est bien le même
indicateur (piège classique : chômage BIT contre demandeurs d'emploi).

### Droit : codes et lois de plusieurs pays

Il n'existe **aucune base mondiale unique** des codes pénaux. Atlas combine des
miroirs locaux pour les juridictions les plus citées et des adaptateurs à la demande
pour les autres.

| Juridiction | Source | Accès | Stratégie |
|---|---|---|---|
| France | **LEGI** (DILA) : 73 codes en vigueur consolidés, lois et règlements | Téléchargement libre (Licence Ouverte 2.0) sur le serveur de la DILA ; API Légifrance gratuite via le portail PISTE (inscription, quotas sur PISTE) | MVP : API PISTE + cache. Ensuite : **miroir local** des codes prioritaires, mis à jour par les diffusions quotidiennes |
| Union européenne | **EUR-Lex / CELLAR** | Point SPARQL et API REST gratuits | À la demande + cache |
| Suisse | **Fedlex** | Point SPARQL ouvert, sans clé, identifiants ELI | À la demande + cache |
| Canada | **Lois du Canada** (Justice) | XML en masse, licence du gouvernement ouvert | Miroir des lois fédérales citées (Code criminel en tête) |
| Belgique, Luxembourg, Québec | Justel, Legilux, LégisQuébec | Identifiants ELI ; modalités d'accès automatisé **à vérifier** | Adaptateur à la demande |
| Autres pays | Portails officiels recensés dans `legal_portals` (pays, URL, prise en charge ELI) | Variable | Recherche web limitée aux domaines officiels du pays |

Miroir local (France) :

- table `legal_articles` : identifiant LEGI, code, numéro d'article, texte, **dates de
  début et de fin de vigueur** (on peut répondre « que disait l'article à la date du
  post ? »), empreinte ;
- recherche plein texte PostgreSQL en français (`tsvector` + index GIN, sur disque,
  peu de RAM) ; embeddings calculés **à la demande** pour les articles effectivement
  consultés, pas pour tout le corpus ;
- codes prioritaires pour commencer : pénal, procédure pénale, civil, travail, entrée
  et séjour des étrangers (CESEDA), électoral, santé publique, sécurité intérieure,
  Constitution ; taille à mesurer avant d'étendre ;
- synchronisation par le service `atlas` : téléchargement en flux, analyse XML
  incrémentale (bibliothèque standard), mise à jour par différence quotidienne.

Le juge reçoit l'article **avec sa date de vigueur** : une affirmation sur une loi
abrogée ou un projet de loi non adopté est un cas typique de besoin de contexte.

## 4. Encyclopédies : Wikipédia

Wikipédia est une source **tertiaire** : excellente carte, mauvais territoire.

- Accès : API REST et Action API publiques. Limites 2026 : 200 requêtes par minute
  pour un client identifié par un User-Agent conforme (avec un contact), 10 pour un
  client non identifié, 3 requêtes simultanées au plus, respect de `Retry-After`.
  Wikimedia Enterprise offre en plus un compte gratuit (de l'ordre de 50 000 requêtes
  à la demande par mois et des instantanés mensuels, chiffres variables selon les
  pages officielles), avec un contenu structuré (infobox analysées) en bêta.
- Usage : 1) contexte général pour le cadrage ; 2) surtout, **suivre les références**
  de l'article vers les sources primaires, qui deviennent les vraies preuves ;
  3) passage cité seulement si aucune source primaire n'est trouvée, avec confiance
  réduite.
- **Révision figée** : on cite un numéro de révision, pas l'article courant.
- **Contrôle du vandalisme** : avant d'utiliser une révision récente (moins de 48 h),
  on interroge le modèle « revert risk » de Wikimedia (Lift Wing, public, indépendant
  de la langue) ; une révision à fort risque d'annulation est remplacée par la
  dernière révision stable.
- Le User-Agent de Tower contient une URL de contact générique de Seat 12A, jamais une
  adresse personnelle.

## 5. Actualité de référence

| Source | Accès | Usage |
|---|---|---|
| Flux RSS d'éditeurs de référence (service public, presse nationale et régionale, agences) | Gratuit | Index local des **titres, chapeaux, dates, URL** des dernières semaines (service `atlas`, toutes les 15 min) ; l'article complet n'est récupéré qu'à la demande |
| **GDELT DOC 2.0** | Gratuit, sans clé ; recherche par mots-clés anglais avec filtre de langue source | Repérer la couverture mondiale d'un événement |
| **AFP** | Pas d'API en libre-service : accès commercial (AFP Connect), sur contrat | Option si le budget le permet ; AFP Factuel est en partie accessible via ClaimReview (niveau 2) |
| Bluesky et autres plateformes | Voir [plateformes-medias.md](plateformes-medias.md) | Détection de vagues, pas preuve |

L'actualité sert surtout aux **situations en cours** (péremption 6 à 24 h) et à dater
un événement. Une dépêche reprise par dix titres compte pour une seule source
(dédoublonnage par SimHash des passages, voir [pipeline.md](pipeline.md)).

## 6. Recherche web générale

- Fournisseur par défaut : **Brave Search API** (environ 5 $ pour 1 000 requêtes, avec
  un crédit gratuit de 5 $ par mois, soit environ 1 000 requêtes). L'outil de recherche
  web d'Anthropic (10 $ pour 1 000) et celui d'OpenAI ne sont pas utilisés : plus chers,
  et ils donneraient un outil au modèle qui lit du contenu non fiable.
- Requêtes d'abord **restreintes aux domaines de niveau primaire** du pays concerné,
  puis élargies si rien n'est trouvé.
- Port `SearchClient` : changer de fournisseur ne touche qu'un adaptateur.

## Registre de fiabilité des sources

Table `source_registry` : éditeur et domaine enregistrable → niveau (primaire,
secondaire, tertiaire, à éviter), type (institution, agence, presse, encyclopédie,
fact-checker, plateforme), pays, motif, origine, date de revue. Versionnée, éditable
par les éditeurs depuis Discord. Deux attributs ajoutés après l'avis de Fable
([reponses-fable.md](reponses-fable.md), Q11) :

- `controlled_by` : l'entité qui contrôle l'éditeur (État, parti, groupe), utilisée
  par la règle fermée des sources partie prenante ([pipeline.md](pipeline.md), E7).
  Ce registre de relations de contrôle est un choix humain : il est **versionné et
  publié**. Il est amorcé automatiquement depuis Wikidata (propriétés « propriétaire »
  et « organisation mère ») et les registres officiels, puis corrigé par l'équipe ;
- `statutory_independence` : un organisme statistique produisant sous indépendance
  statutaire voit ses chiffres acceptés seuls. L'attribut repose sur des **critères
  externes** plutôt que sur un jugement de l'équipe. Candidats, **à vérifier** :
  adhésion aux normes de diffusion des données du FMI, revues par les pairs
  d'Eurostat.

L'indépendance entre sources se mesure **par origine** (l'entité à qui l'information
est attribuée), pas par domaine : deux journaux qui relaient le même communiqué
comptent pour une seule source.

| Niveau | Exemples | Critère |
|---|---|---|
| Primaire | INSEE, Eurostat, Légifrance, Journal officiel, data.gouv, EUR-Lex, Fedlex, sites des ministères et juridictions, publications scientifiques évaluées | Producteur officiel de la donnée ou du texte |
| Secondaire | Agences de presse, presse de référence, fact-checkers signataires de l'IFCN ou membres de l'EFCSN | Processus éditorial, politique de correction publique |
| Tertiaire | Wikipédia, agrégateurs, blogs, réseaux sociaux | Le reste |
| À éviter | Sites de désinformation documentés | Décision éditoriale motivée |

Règles de neutralité :

- le niveau se fonde sur le **processus éditorial** (producteur officiel, correction
  publique, transparence), **jamais sur l'orientation politique** ;
- l'Observatoire des sources de Wikipédia en français (environ 250 sources commentées,
  sans grille fixe ni format machine) et la liste des sources récurrentes de Wikipédia
  en anglais servent de **point de départ**, revu par l'équipe, pas de vérité ;
- la distribution des niveaux par orientation des médias est auditée hors pipeline
  ([biais.md](biais.md)).

## Droit d'auteur et conservation

- Pages récupérées : on conserve **toujours le lien de la source** (URL canonique et
  URL d'origine), le titre, l'éditeur, la date de publication et de récupération,
  l'empreinte SHA-256, et **seulement les passages cités** (droit de courte citation).
  Pour lutter contre la disparition des pages, un lien d'archive publique peut être
  demandé pour chaque source citée (candidat : Internet Archive ; conditions d'usage
  automatisé **à vérifier**) ; si la page source disparaît, le lien d'archive prend le
  relais. Le texte complet des pages non
  citées est supprimé après l'enquête (cohérent avec l'article L. 122-5-3 du code de
  la propriété intellectuelle, qui impose de détruire les copies faites pour la fouille
  de textes, hors recherche).
- On respecte `robots.txt` et les réservations de fouille exprimées par les éditeurs
  (opt-out prévu par le même article).
- Données sous licence ouverte (LEGI, INSEE, Eurostat, Wikidata en CC0, Wikipédia en
  CC BY-SA) : licence enregistrée avec chaque preuve, attribution dans les brouillons
  publiés.

## Coût de l'Atlas

Toutes les sources des niveaux 1 à 4 sont gratuites. Le coût marginal est celui des
recherches web (niveau 6) et, éventuellement, d'un contrat AFP. L'objectif mesurable :
plus de la moitié des affirmations N1 et N2 tranchées sans recherche web payante.
