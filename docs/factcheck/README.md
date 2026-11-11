# Fact-check : plan d'implémentation

Plan concret d'intégration du fact-checker dans 12A Tower, rédigé le 2026-11-10. Il part
du plan de Fable (cascade E0 à E8, affirmations plutôt que posts, verdict aveugle,
profils, budgets) et le rend **implémentable** dans le socle existant. Il intègre les
demandes de Jules :

- deux fournisseurs : **OpenAI** pour les petits modèles très sollicités (tokens
  gratuits), **Anthropic** pour le raisonnement ;
- des **sources de connaissance intégrées** (Wikipédia, Wikidata, codes juridiques de
  plusieurs pays, statistiques publiques, actualité, fact-checks existants), pour ne
  pas tout faire passer par des recherches web ;
- des modèles qui **prennent du recul** et comprennent le contexte ;
- un **système de notes de contexte** en plus du fact-check ;
- des **niveaux de traitement** : un tweet anonyme non viral ne reçoit pas la même
  puissance de calcul qu'un politicien ou un contenu viral ;
- le traitement des **vagues de rumeurs** qui explosent en ligne ;
- une couverture **mondiale** : des contenus dans toutes les langues et des
  affirmations sur tous les pays, pas seulement la France et le français ;
- **d'autres plateformes**, puis les **images et vidéos**, prévues dès maintenant ;
- **coût minimal**, en argent comme en **RAM** (Railway facture environ 10 $ par Go et
  par mois) ;
- propre, compréhensible, **scalable dans toutes les directions**.

Toutes les affirmations factuelles (tarifs, API, études) sont sourcées dans
[recherche.md](recherche.md), avec leur date de vérification. Ce qui n'a pas pu être
vérifié est signalé « à vérifier ».

## En bref

1. **On vérifie des affirmations et des récits, pas des posts.** Les milliers de
   variantes d'une même rumeur se ramènent à un groupe d'affirmations, puis à un
   **récit**. Un récit partage un dossier de preuves ; chaque variante reçoit son
   propre verdict, calculé à bas coût sur ce dossier.
2. **Le moins cher est l'absence de modèle, puis le token gratuit.** Le code
   (normalisation, empreintes, classifieur en Python pur) écarte la majorité du flux.
   Les petits rôles (extraction, arbitrage, vérification légère, rédaction des
   requêtes, second avis aveugle) passent par les modèles OpenAI couverts par le programme de
   tokens gratuits. Anthropic n'intervient que pour le jugement et la rédaction.
3. **Connaissance structurée d'abord, recherche web en dernier.** Un étage
   « Atlas » interroge dans l'ordre : nos fiches de faits, les fact-checks existants
   (ClaimReview), les sources structurées par type (Wikidata, INSEE, Eurostat,
   Légifrance, EUR-Lex, Fedlex…), Wikipédia, l'actualité, puis seulement la recherche
   web payante.
4. **Prendre du recul est un étage à part entière.** Avant l'enquête, un étage de
   **cadrage** établit ce que l'affirmation dit vraiment, ses sous-entendus, les
   définitions en jeu, la période, l'interprétation la plus charitable. Chaque issue
   peut porter une **note de contexte**, indépendante du verdict de véracité.
5. **Cinq niveaux de traitement**, de N0 (écarté) à N4 (critique). Le niveau dépend de
   la portée, de la vélocité, du rôle public de l'auteur (jamais de son bord) et du
   risque thématique. Il monte et descend automatiquement.
6. **Mode vague.** Quand un récit accélère anormalement, il passe en voie rapide :
   enquête unique, brouillon prioritaire, application à toutes les variantes,
   signalement des signaux de coordination.
7. **Aucun modèle local, aucune bibliothèque lourde.** Embeddings par API (moins de
   1 $ par mois au volume supposé, contre 20 à 25 $ par mois de RAM pour un modèle
   local de type bge-m3). Calculs vectoriels dans PostgreSQL (pgvector, `halfvec`).
   Les réplicas de fact-check restent des workers asyncio légers.
8. **Mondial par construction.** Chaque affirmation porte sa langue et sa
   juridiction (code pays ISO). Le routeur de sources choisit les sources du pays
   concerné dans des registres par pays (statistique, droit, actualité,
   fact-checkers). Les embeddings sont multilingues, donc une rumeur traduite est
   reconnue comme la même. Les notes sont rédigées dans la langue du post, avec une
   traduction française pour l'équipe. Détail : [international.md](international.md).
9. **Tout est enfichable.** Fournisseur de modèle, source de connaissance, plateforme,
   type de média : chacun est un adaptateur derrière un port de `tower.core`. Ajouter
   un pays, une plateforme ou une source revient à ajouter un adaptateur et une ligne
   de configuration.
10. **Tout est versionné et rejouable.** Profils, routes de modèles, prompts, registre
   des sources : chaque traitement enregistre les versions utilisées. Les appels aux
   modèles sont mis en cache par contenu : un rejeu ne coûte rien.

Coût estimé en régime établi, au volume supposé : **environ 63 à 91 $ par mois** d'IA
et de recherche selon le taux d'escalade (77 $ à 25 %), hors API X (87 $ dans le plan
de Fable). Le flux de masse passe d'environ 20 $ à environ 2 $ ; l'économie est
réinvestie dans le cadrage et la rédaction. Il faut y ajouter environ 2,5 $ par mois de
RAM pour un réplica de fact-check. Si le programme de tokens gratuits s'arrête :
environ +25 $ par mois. Détail : [couts.md](couts.md).

**Arbitrages de Fable intégrés** ([reponses-fable.md](reponses-fable.md)) : profondeur
d'enquête uniforme avec escalade par difficulté (le rôle public n'achète que la
surveillance et la latence), second avis aveugle au lieu d'un contradicteur, lots de
10 avec préfixe court, verdicts indexés par fiche et bande de valeurs, récits qui
partagent des preuves et non des verdicts, détection de vague par part du flux,
notes de contexte rattachées à des objets, indépendance des sources mesurée par
origine.

## Objectivité, pas faux équilibre

Seat 12A est **objectif**, pas « neutre à mi-chemin » : mêmes règles pour tout le
monde, verdict dicté par les preuves, et aucune fausse symétrie sur les **faits
établis** (crimes du nazisme, génocides reconnus, consensus scientifiques), définis
par des critères fermés et un registre publié. Voir [objectivite.md](objectivite.md).

## Sommaire

| Page | Contenu |
|---|---|
| [niveaux.md](niveaux.md) | Les cinq niveaux de traitement, promotion, rétrogradation, budgets |
| [pipeline.md](pipeline.md) | Étages E0 à E11, entrées et sorties, intégration dans Tower (services, files, jobs, topics) |
| [vagues.md](vagues.md) | Récits, détection des vagues de rumeurs, mode vague, tenue en charge |
| [modeles.md](modeles.md) | Routage OpenAI et Anthropic par rôle, tokens gratuits, repli, contrats de sortie |
| [connaissances.md](connaissances.md) | Atlas : sources intégrées (encyclopédie, droit, statistiques, actualité, fact-checks), registre de fiabilité |
| [objectivite.md](objectivite.md) | Objectivité plutôt que faux équilibre : égalité de traitement, faits établis (registre à critères fermés), garde-fous |
| [international.md](international.md) | Couverture mondiale : langues, juridictions, registres de sources par pays, relecture multilingue |
| [contexte.md](contexte.md) | Issues, notes de contexte, étage de cadrage (prendre du recul), format des brouillons |
| [biais.md](biais.md) | Biais des modèles et des humains, injection, neutralité : parades concrètes |
| [plateformes-medias.md](plateformes-medias.md) | Multi-plateforme (X, Bluesky, Threads…) et futur multimodal (images, vidéos) |
| [donnees.md](donnees.md) | Modèle de données : tables, index, rétention, migrations |
| [couts.md](couts.md) | Coûts par niveau, projection mensuelle, RAM, sensibilité |
| [evaluation.md](evaluation.md) | Jeu de référence, métriques, tests de neutralité, rejeu |
| [feuille-de-route.md](feuille-de-route.md) | Lots de livraison, critères chiffrés de passage |
| [questions.md](questions.md) | Questions pour Jules |
| [questions-fable.md](questions-fable.md) | Brief et questions d'architecture soumis à Fable |
| [reponses-fable.md](reponses-fable.md) | Réponses de Fable (2026-11-10), archivées |
| [prompts/partie-1-fondations-ingestion.md](prompts/partie-1-fondations-ingestion.md) | Prompt d'implémentation de la partie 1 (fondations et ingestion) pour une session Claude Code |
| [recherche.md](recherche.md) | Sources consultées, ce qu'on en retient, dates de vérification |

## Écarts avec le plan de Fable

| Sujet | Plan de Fable | Ce plan | Raison |
|---|---|---|---|
| Fournisseurs | Anthropic seul | OpenAI (petits rôles, tokens gratuits) + Anthropic (jugement, rédaction) | Tokens gratuits OpenAI ; second avis d'un autre fournisseur, donc erreurs décorrélées |
| Embeddings | Modèle local (bge-m3, e5) sur CPU | API OpenAI `text-embedding-3-small`, 512 dimensions | Sur Railway, 2 Go de RAM coûtent environ 20 $/mois ; l'API coûte moins de 1 $/mois au volume supposé |
| Sources | Recherche web + fiches de référence | Atlas : sources structurées intégrées avant toute recherche web | Coût, fiabilité ; une étude 2025 montre que le contexte curé surpasse la recherche web (voir [recherche.md](recherche.md)) |
| Contexte | Issue « Contexte manquant » | Axe séparé : note de contexte typée, possible quel que soit le verdict | Demande de Jules ; modèle des notes de la communauté |
| Recul | Implicite dans le juge | Étage de cadrage dédié (E6) | Les erreurs viennent souvent d'une mauvaise lecture de l'affirmation |
| Niveaux | 4 profils (Masse, Viral, Ciblé, Critique) | 5 niveaux N0 à N4, mêmes profils + N0 explicite, rétrogradation | Lisibilité, coût ; le niveau est la clé de tous les réglages |
| Rumeurs | Groupes d'affirmations | Groupes + récits + mode vague | Demande de Jules : traiter une rumeur qui explose |
| Triage | Haiku par lot | Lots de 10 posts (plafond 12) par appel OpenAI, préfixe de 900 tokens, composition par dissemblance | Divise par 5 environ les tokens consommés ; tient dans le quota gratuit |
| Plateformes | X | Modèle générique `content_items` + adaptateurs | Autres plateformes prévues |
| Périmètre géographique | Francophone, sources françaises | Toutes langues, tous pays : juridiction par affirmation, registres de sources par pays | Demande de Jules |
| Médias | Hors périmètre | Table `media_assets` et étages M0 à M2 prévus, livrés plus tard | Images et vidéos prévues |
| Audit | Fable 10 $ / 50 $ | Fable 5.2 à 8 $ / 40 $ | Tarif actuel |

Les principes de Fable restent tous valables : affirmations canoniques, signature à
sept champs, réutilisation déterministe, péremption par type, collecte hors modèle,
juge sans outil, verdict aveugle, deux sources indépendantes dont une primaire pour
« Vrai » ou « Faux », quota de brouillons calé sur la capacité de relecture.

## Ce que ce plan ne change pas dans le socle

Les règles de `CLAUDE.md` s'appliquent sans exception : cas d'usage pour toute
écriture, événements dans la même transaction, services qui n'importent que
`tower.core`, appels externes par les ports, handlers idempotents, abonnements courts,
statuts en `TEXT` + `CHECK`, migrations expand/contract, aucune publication
automatique, aucun scraping de X.
