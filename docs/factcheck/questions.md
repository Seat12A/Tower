# Questions ouvertes

## Pour Jules

Classées par effet sur la conception, de la plus structurante à la moins structurante.

1. **Capacité de relecture réelle** (personnes, heures par semaine) : elle fixe le
   quota de brouillons, donc toute la dépense d'enquête.
2. **Partage des données avec OpenAI** : accepter que les posts publics et pages web
   envoyés aux petits modèles servent à l'entraînement d'OpenAI ? Les posts contiennent
   des données personnelles de leurs auteurs (RGPD) ; un avis juridique est conseillé
   avant d'activer le programme. Alternative : organisation sans partage, coût
   d'environ +19 $ par mois.
3. **Palier d'usage OpenAI** de l'organisation et **liste exacte des modèles
   éligibles** à l'allocation gratuite (famille GPT-6 comprise, API Batch et Flex
   comprises ?), à relever dans le tableau de bord.
4. **Application automatique des verdicts** à tout un groupe ou un récit sur le site et
   l'extension, sans revue post par post ? Si oui, une erreur de regroupement devient
   publique et les seuils doivent monter.
5. **Volumes réels** : posts observés par jour, nombre de comptes suivis et posts par
   jour de ces comptes (le coût de l'API X en dépend directement).
6. **Budget mensuel** et inclusion ou non de l'API X.
7. **Liste des comptes suivis** : catégories de rôle public retenues (seuil
   d'abonnés ?), qui la tient, publication.
8. **Étiquetage politique des comptes** (pour mesurer la neutralité) : qui l'établit,
   selon quelle grille, sur quelle base légale (opinions politiques : donnée sensible
   au sens de l'article 9 du RGPD, même déduite de comptes publics) ?
9. **Contrat AFP** : envisageable ? Sinon, l'actualité repose sur les flux publics,
   GDELT et ClaimReview.
10. **Latence acceptable** sur un contenu viral (2 h prévues) et en vague (30 min
    prévues).
11. **Langues et pays prioritaires** : quelles langues traiter dès le départ (le
    système est mondial, mais chaque langue demande un sous-ensemble de référence) ?
    Quels pays passer en couverture A en premier ? Quelles langues l'équipe lit-elle
    pour relire et publier ailleurs qu'en français ?
12. **Plateformes prioritaires** après X (proposition : Bluesky, puis Threads).
13. **Images et vidéos** : à quel horizon ? Le modèle de données les prévoit déjà.
14. **Identité des installations de l'extension** : jeton anonyme stable possible ?
    Sans cela, le critère « observateurs indépendants » n'est pas mesurable.
15. **Conservation des pages sources** : le plan propose de ne garder que les passages
    cités et l'empreinte de la page. À confirmer.
16. **Brouillons pièges** pour mesurer la vigilance des relecteurs : l'équipe est-elle
    d'accord sur le principe ?

## À soumettre à Fable (architecture)

Points où un second avis serait utile avant l'implémentation :

1. **Niveau N3 et neutralité** : le plan autorise le **rôle public** de l'auteur (élu,
   ministre, candidat, média) pour fixer la profondeur de vérification, tout en
   interdisant le bord. Le juge ne voit jamais le niveau. Est-ce suffisant, ou faut-il
   une contrainte supplémentaire (par exemple un quota de traitement par bord mesuré
   a posteriori) ?
2. **Micro-lots en extraction** (20 posts par appel) : le gain de tokens est d'environ
   × 5. Le risque est la contamination croisée et l'injection d'un post sur les
   autres. Les parades prévues (IDs, délimitation, marquage, contrôle de fidélité, test
   dédié) suffisent-elles, ou faut-il plafonner la taille des lots en dessous de 20 ?
3. **Contradicteur d'un autre fournisseur** (OpenAI) que le juge (Anthropic) : l'idée
   est de décorréler les erreurs. Y a-t-il un risque de désaccords systématiques dus
   aux différences de style plutôt que de fond, qui feraient baisser la confiance
   inutilement ? Comment le calibrer ?
4. **Récits** : rattachement incrémental par centroïde, co-occurrence et entités.
   Quelle règle de fusion de récits évite les « récits fourre-tout » (par exemple tout
   ce qui parle d'immigration) tout en reconnaissant une rumeur qui mute ?
5. **Identifiants multi-plateformes** : `external_id TEXT` + colonne générée
   `x_post_id BIGINT` pour respecter la règle « IDs X en BIGINT ». Alternative : une
   table par plateforme. Le choix proposé est-il le bon compromis ?
6. **Détection de vague** par z-score robuste sur l'historique du thème : pertinent
   pour un service qui démarre sans historique ? Faut-il des seuils absolus au début ?
7. **Échantillonnage sous charge** pendant une vague : risque de rater une variante
   importante (nouvelle affirmation dans le récit) ? Faut-il forcer le passage des
   posts dont l'embedding s'éloigne du centroïde ?

## Points à vérifier avant implémentation (techniques)

- pgvector ≥ 0,8 disponible sur Railway.
- Quotas réels : PISTE (Légifrance), INSEE, Google Fact Check Tools.
- Accès programmatique à Justel (Belgique), Legilux (Luxembourg), LégisQuébec,
  EUvsDisinfo.
- Tarif exact de transcription (`gpt-4o-mini-transcribe`) au moment du lot médias.
- Plafond mensuel de lectures de l'API X (2 ou 3 millions selon les sources).
- RSS réel des réplicas avec les SDK `openai` et `anthropic` chargés.
