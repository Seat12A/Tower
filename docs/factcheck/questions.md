# Questions ouvertes

## Pour Jules : réponses du 2026-11-11

| # | Question | Réponse | Effet dans le plan |
|---|---|---|---|
| 1 | Capacité de relecture | On s'adaptera ; ne pas tout envoyer, une cinquantaine de brouillons par jour au plus | Plafond de 50 brouillons/jour, sélection par doute et portée ([niveaux.md](niveaux.md)) |
| 2 | Partage des données avec OpenAI | Oui | Partage activé, minimisation stricte ; avis juridique recommandé ([modeles.md](modeles.md)) |
| 3 | Palier OpenAI | Palier 1, environ 15 € crédités | Allocation de 2,5 M / 250 k tokens par jour ; repli payant au-delà |
| 4 | Volumes | Peu au début (une dizaine de tweets par jour), montée possiblement rapide | Cadence adaptative de l'ordonnanceur ([pipeline.md](pipeline.md), E4) |
| 5 | Budget | Le moins possible | Plafond par défaut de 30 $/mois, dégradation automatique ([couts.md](couts.md)) |
| 6 | Application automatique des verdicts | Oui | Seulement aux variantes de signature identique ou de bande validée |
| 7 | Comptes suivis | L'équipe tient la liste, choix libre | Choix éditorial ; publication recommandée ; la liste ne change que surveillance et latence |
| 8 | Étiquetage politique | « Choisis le mieux » | Choix fait : étiquettes limitées aux partis, organisations et affiliations officielles d'élus et de candidats ; jamais d'étiquette déduite pour un compte ordinaire ; table séparée lue par l'analytique seule |
| 9 | Méthodologie publique (pouvoir en place plus vérifié) | Oui | À écrire dans la méthodologie publiée |
| 10 | Registre de contrôle des sources | Question pas claire | Choix fait : amorcé depuis Wikidata et registres officiels, corrigé par l'équipe, publié ([connaissances.md](connaissances.md)) |
| 11 | Latence | Valeur qui s'adapte au nombre de tweets | Cadence `borne(60 s, 30 min, 5 ÷ débit)`, réveil immédiat pour N3/N4 |
| 12 | Brouillons pièges | Ne sait pas | Choix fait : non activés au départ ; envisagés si l'acceptation sans modification dépasse 95 % sur un trimestre |
| 13 | Langues | Équipe : français et anglais ; réponse dans la langue du tweet ; tout traduisible, langue au choix dans l'extension | Section « Langues de publication » ([international.md](international.md)) |
| 14 | Plateformes | Pas encore choisies, mais le système doit déjà les distinguer | Plateforme portée par chaque contenu, compte, observation et événement dès le lot 2 ([plateformes-medias.md](plateformes-medias.md)) |
| 15 | Images et vidéos | Ne sait pas | Choix fait : lot 8 ; empreinte des images et liens des médias conservés dès le lot 2 |
| 16 | Contrat AFP | Non pour l'instant | Flux publics, GDELT, ClaimReview |
| 17 | Jeton d'installation | Oui, sans aucune possibilité de profilage ou de traçage de notre côté | Pseudonyme du jour, secret détruit chaque jour, aucune IP stockée ([donnees.md](donnees.md)) |
| 18 | Conservation des pages | Ne sait pas, mais les liens des sources doivent être conservés | Choix fait : liens toujours conservés + passages cités + empreinte ; lien d'archive publique à étudier |

### Encore à confirmer par Jules

- **Publication sans relecture** : le plan suppose que **rien n'est publié sans
  relecture humaine** et que le plafond de 50 par jour sert à choisir ce qui est relu.
  Si l'intention est de publier sans relecture les verdicts jugés sûrs, il faut le
  dire : cela change le cadre légal (étiquetage « généré par IA » obligatoire, AI Act
  article 50) et le risque d'erreur publique.
- Publication de la liste des comptes suivis : recommandée, pas encore décidée.

## À soumettre à Fable (architecture)

Brief autonome avec contexte et onze questions précises :
[questions-fable.md](questions-fable.md). Réponses du 2026-11-10 :
[reponses-fable.md](reponses-fable.md), intégrées au plan.

Points restés ouverts après ces réponses :

- liste des critères externes d'indépendance statutaire des organismes statistiques
  (normes de diffusion du FMI, revues par les pairs d'Eurostat : à vérifier) ;
- constitution et publication du registre des relations de contrôle
  (`controlled_by`) : qui le tient, selon quelles sources ;
- vérification de la langue pivot étudiée dans SemEval-2025 n° 7 ;
- rédaction de la méthodologie publique (dont : « le pouvoir en place sera plus
  vérifié, quel qu'il soit »).

## Points à vérifier avant implémentation (techniques)

- pgvector ≥ 0,8 disponible sur Railway.
- Quotas réels : PISTE (Légifrance), INSEE, Google Fact Check Tools.
- Accès programmatique à Justel (Belgique), Legilux (Luxembourg), LégisQuébec,
  EUvsDisinfo.
- Tarif exact de transcription (`gpt-4o-mini-transcribe`) au moment du lot médias.
- Plafond mensuel de lectures de l'API X (2 ou 3 millions selon les sources).
- RSS réel des réplicas avec les SDK `openai` et `anthropic` chargés.
