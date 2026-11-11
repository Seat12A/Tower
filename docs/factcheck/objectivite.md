# Objectivité, pas faux équilibre

Principe fixé par Jules (2026-11-11) : Seat 12A doit être **objectif**, pas
parfaitement neutre. Sur des sujets comme le nazisme, le système **ne prend pas une
position intermédiaire**.

## Ce que « neutre » veut dire dans ce projet, et ce qu'il ne veut pas dire

| Oui : égalité de traitement | Non : faux équilibre |
|---|---|
| Les mêmes règles, les mêmes seuils et la même profondeur d'enquête pour tout le monde, quel que soit l'auteur ou son bord | Chercher à donner raison à « chaque camp » ou présenter deux positions comme équivalentes quand les preuves ne le sont pas |
| Le verdict suit les preuves, et seulement les preuves | Équilibrer artificiellement les verdicts entre bords : si un camp diffuse plus de faux à portée égale, les verdicts le refléteront |
| Le juge ne voit ni l'auteur, ni son bord, ni l'engagement | S'abstenir (« Non vérifiable ») sur un fait établi par peur de paraître partial |
| Formulation factuelle, sans adjectif de jugement sur les personnes | Taire une qualification juridique ou historique établie (« génocide », « crime contre l'humanité ») par prudence |

La règle 10 de `CLAUDE.md` (aucune logique de priorité ou de verdict ne dépend de
l'auteur ni de son bord) reste entière : c'est l'égalité de traitement. Ce qui est
écarté, c'est l'idée que l'objectivité consisterait à se placer au milieu.

## Les faits établis

Certains faits ne sont pas en débat : les nier est faux, pas « une autre opinion ».
Le système les traite comme tels.

### Critères fermés

Un fait est **établi** s'il remplit au moins un de ces critères, documenté par des
sources :

1. **Décision de justice** d'une juridiction internationale ou d'une juridiction
   nationale d'un État de droit, définitive : Tribunal de Nuremberg, Tribunaux pénaux
   internationaux pour le Rwanda et l'ex-Yougoslavie, Cour internationale de justice,
   Chambres extraordinaires au sein des tribunaux cambodgiens, etc.
2. **Consensus scientifique** exprimé par les organismes de référence du domaine
   (académies des sciences, GIEC, agences sanitaires internationales et nationales),
   pas par une étude isolée.
3. **Consensus historiographique** documenté dans les ouvrages et institutions de
   référence (par exemple : Mémorial de la Shoah, Yad Vashem, United States Holocaust
   Memorial Museum).

Ces critères sont les mêmes **quelle que soit l'idéologie** concernée. Exemples
d'entrées : la Shoah et les crimes du régime nazi ; le génocide des Tutsi au Rwanda ;
le massacre de Srebrenica qualifié de génocide ; le génocide perpétré par les Khmers
rouges ; le caractère anthropique du réchauffement climatique ; l'efficacité de la
vaccination contre la rougeole. Le génocide arménien, l'Holodomor ou la famine du
Grand Bond en avant relèvent du même registre (consensus historiographique), avec des
qualifications juridiques variables selon les pays, à indiquer précisément.

### Registre `established_facts`

Table versionnée et **publiée** : énoncé du fait, critère (justice, science,
histoire), sources (décisions, rapports, ouvrages de référence), qualifications
juridiques par pays quand elles diffèrent, date de revue, auteur. Tenue par l'équipe
éditoriale, éditable depuis Discord, chaque modification journalisée. L'ajout d'une
entrée exige au moins une source répondant à un critère ci-dessus ; une simple
majorité d'opinion ne suffit pas.

### Effet dans le pipeline

| Étage | Effet |
|---|---|
| E3 / E5 | Une affirmation qui contredit un fait établi est reconnue (signature, embedding, fiche du registre) |
| E8 | Le juge reçoit l'entrée du registre et ses sources comme preuves de premier rang. Issue « Faux » (ou « Trompeur » pour une minimisation) ; **l'abstention n'est pas permise** sur le cœur du fait établi. Les sources du registre satisfont l'exigence de deux origines indépendantes dont une primaire |
| E9 | La note énonce le fait sans détour (« Le génocide des Juifs d'Europe par le régime nazi est établi par… »), avec les sources. Les qualifications juridiques et historiques établies sont **autorisées** ; seuls les adjectifs éditoriaux sur des personnes restent proscrits |
| Second avis | Un désaccord sur un fait établi ne fait pas baisser la confiance : il est journalisé comme erreur du modèle concerné |
| Modèles | Les consignes interdisent explicitement la fausse symétrie (« certains disent… d'autres disent… ») quand les preuves sont univoques |

## Négation, apologie et haine : au-delà du fact-check

Nier ou contester des crimes contre l'humanité, en faire l'apologie ou inciter à la
haine peut être un délit (en France, notamment la loi du 29 juillet 1881 sur la
liberté de la presse). Ces contenus relèvent aussi de la **modération** (service
`moderation`, à définir) : le fact-check énonce les faits, et le contenu est signalé à
l'équipe pour un traitement de modération. Le système ne qualifie pas juridiquement
une personne : il signale.

## Les garde-fous : pour que l'objectivité ne devienne pas un biais

L'exception « pas de neutralité » est puissante, donc strictement bornée :

1. **Elle porte sur des faits, pas sur des personnes ou des partis actuels.** Dire que
   le régime nazi a commis un génocide est un fait établi. Qualifier un responsable
   politique actuel de « nazi » est une opinion, sauf décision de justice : le système
   ne le fait pas. Il rapporte des faits vérifiables sur lui (déclarations
   documentées, condamnations définitives), comme pour n'importe qui.
2. **Critères fermés et symétriques** : mêmes critères pour les crimes de tous les
   régimes et pour les consensus scientifiques, quel que soit le camp que cela
   arrange.
3. **Registre publié et sourcé** : chacun peut voir ce que Seat 12A tient pour établi,
   et pourquoi.
4. **Hors registre, les règles normales s'appliquent** : une question historique ou
   scientifique réellement débattue reste traitée avec nuance, « Non vérifiable » ou
   note `scientific_uncertainty` compris.
5. **Mesure** : le jeu de référence contient des cas de négation de faits établis (de
   toutes idéologies), où une réponse « équilibrée » ou une abstention compte comme une
   **erreur**, et des cas réellement débattus où un verdict tranché compte comme une
   erreur ([evaluation.md](evaluation.md)).

## Refus des modèles

Les modèles refusent parfois de traiter les contenus liés au nazisme, au terrorisme
ou aux génocides. Un refus n'est jamais converti en verdict : repli vers un autre
modèle, sinon signalement au relecteur ([modeles.md](modeles.md)).
