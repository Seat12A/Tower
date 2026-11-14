# Prompt d'implémentation : lot 6 (V3 du fact-check, Atlas complet et qualité)

À coller dans une nouvelle session Claude Code (Opus) sur le dépôt `Seat12A/Tower`.
Rédigé le 2026-11-14, après le lot 5 et la vérification des contenus de l'extension.
Les règles permanentes (autonomie, sous-agents facultatifs, économie d'usage, RAM,
coût, définition de « terminé ») sont dans `CLAUDE.md` : elles ne sont pas répétées
ici et s'appliquent telles quelles.

---

Tu es l'agent principal chargé d'implémenter le **lot 6 : la V3 du fact-check**. Il
complète l'Atlas : statistiques européennes et internationales, droit de plusieurs
juridictions, actualité, registre de fiabilité éditable, registres par pays. Il pose
aussi l'outillage de qualité (recalibrage de la confiance, distillation du classifieur
E1), qui ne sera **activé** qu'avec des données réelles. Jules peut être absent :
décide, note dans `docs/decisions.md`, avance.

## 1. Branche et environnement

- Si la pull request Seat12A/Tower#7 est fusionnée, pars de `main` ; sinon, pars du
  dernier commit de `claude/keen-cerf-vorbzg`, qui contient les lots 4 et 5 et la
  vérification des contenus. Vérifie d'abord sur le dépôt distant qu'aucune branche plus
  récente ne porte déjà une partie du lot 6. Travaille sur la branche indiquée par la
  session.
- **PostgreSQL 16 avec pgvector ≥ 0.8** est nécessaire depuis le lot 4. S'il manque dans
  le conteneur, suis `docs/development.md` : paquet `postgresql-server-dev-16`, puis
  compilation de pgvector v0.8.1 depuis les sources, dans le répertoire temporaire de la
  session. Le paquet Debian `postgresql-16-pgvector` (0.6) est trop ancien.
- Base de test : `TOWER_DATABASE_URL=postgresql://postgres:<mot de passe>@localhost:5432/tower_test`
  (ou le port 5433 par défaut des tests). Lance la suite complète **avant** de toucher au
  code : 1 798 tests verts attendus.
- Smoke tests : gate en `TOWER_HOST=0.0.0.0` et `curl --noproxy '*'` sur `127.0.0.1` (le
  bac à sable ne joint pas l'écoute IPv6). Wikimédia peut limiter l'IP de sortie
  partagée (429, puis 403) : ce n'est pas un bug de Tower.

## 2. Lire d'abord (sections utiles seulement)

1. `CLAUDE.md`, puis les résumés `docs/sessions/2026-11-12-lot-5-v2.md` et
   `docs/sessions/2026-11-14-verification-extension.md` (points ouverts).
2. `docs/fact-check.md` : « Atlas minimal », puis les sections des lots 4 et 5.
3. Le plan :
   - `docs/factcheck/feuille-de-route.md` (lot 6) ;
   - `docs/factcheck/connaissances.md` (ordre de résolution, contrat d'une source,
     statistiques, droit, actualité, registre de fiabilité, droit d'auteur, coût) ;
   - `docs/factcheck/international.md` (couverture A/B, registres par pays, langues) ;
   - `docs/factcheck/donnees.md` (tables `legal_articles`, `legal_portals`,
     `jurisdiction_sources`, `news_feeds`, `election_calendar`, `news_index`, rétention) ;
   - `docs/factcheck/pipeline.md` (E7 : partie prenante, indépendance par origine ;
     confiance et recalibrage) ;
   - `docs/factcheck/evaluation.md`.
4. `docs/decisions.md` : sections « Lot 3 » à « Vérification des contenus de
   l'extension », et « Périmètre après le lot 5 » (lots 7 et 8 reportés, lot 9 traité
   comme un audit).
5. Le code existant de l'Atlas : `core/adapters/knowledge/` (adaptateurs, cache
   `source_cache`, sélecteur, registre amorcé). Reprends ses conventions.

## 3. Périmètre

### Inclus

| Brique | Contenu |
|---|---|
| **Correctifs préalables** | Points ouverts des lots 4 et 5, à traiter en premier : brouillon assemblé après la fusion de son groupe (le filtrer dès l'assemblage) ; chemin des copies N3 qui n'écrit pas dans `content_extractions` ; échec intermittent de `test_end_to_end_examples_with_fake_provider_and_cache` (trouver la cause, ne pas le désactiver) ; mention d'un rôle Discord configurable dans l'alerte de vague |
| **Statistiques** | Adaptateurs **Eurostat** (API Statistics, JSON-stat 2.0), **Banque mondiale**, **OCDE**, **FMI** (SDMX). Chacun renvoie une **valeur structurée** (indicateur, variante de définition, valeur, unité, période, territoire, date de publication), jamais du texte. Ils alimentent les fiches de référence du lot 4. Vérifie chaque API dans sa documentation officielle et consigne-le dans la docstring (« Verified ») ; une API introuvable ou payante est écartée et notée |
| **Droit** | **Miroir LEGI** des codes prioritaires français (pénal, procédure pénale, civil, travail, CESEDA, électoral, santé publique, sécurité intérieure, Constitution) dans `legal_articles`, avec les **dates de vigueur** et une recherche plein texte (`tsvector` français + GIN). Synchronisation par différences quotidiennes, analyse XML **en flux** (bibliothèque standard, mémoire bornée). Mesure la taille réelle avant d'étendre. Adaptateurs à la demande avec cache : **EUR-Lex** (SPARQL/REST), **Fedlex** (SPARQL, ELI), **Lois du Canada**. Le juge reçoit toujours l'article **avec sa date de vigueur** |
| **Actualité** | `news_feeds` (flux de référence par pays et langue, amorcés par une commande) et `news_index` (titre, chapeau, date, URL, éditeur ; rétention 90 jours) alimentés toutes les 15 minutes ; l'article complet n'est récupéré qu'à la demande. Adaptateur **GDELT DOC 2.0** (sans clé). Une dépêche reprise par dix titres compte pour une seule source (SimHash des passages) |
| **Registre de fiabilité éditable** | `source_registry` versionné : modification par cas d'usage (permission éditeur, audit, événement), depuis l'API et le bot. Attributs `controlled_by` (amorcé depuis Wikidata, propriétés « propriétaire » et « organisation mère », puis corrigé par l'équipe ; **versionné et publiable**) et `statutory_independence` (critères externes seulement). Règle fermée des sources partie prenante (E7) branchée sur `controlled_by`. Le niveau dépend du **processus éditorial, jamais de l'orientation politique** |
| **Registres par pays** | `jurisdiction_sources`, `legal_portals`, `election_calendar` ; le sélecteur de l'Atlas choisit les sources selon la juridiction de l'affirmation ; pays absent : recherche web limitée aux domaines officiels du pays |
| **Qualité (outillage)** | Commande de **recalibrage de la confiance** à partir des revues (ordonnée et 3 ou 4 coefficients avec rétrécissement à partir de 200 revues, modèle complet à partir de 100 refus) : elle **refuse** de produire un modèle sous ces seuils et n'active rien sans mesure. Même logique pour la **distillation du classifieur E1** (`train-router` existe : ajouter la mesure de rappel et le critère « moitié des appels E2 sans perte de rappel au-delà de 2 points »). Évaluation étendue : part des affirmations tranchées sans recherche payante, écart de calibration |
| **N4 complet** | Ce qui manque au niveau N4 d'après `niveaux.md` et `pipeline.md` (à établir en lisant le code ; ne rien réécrire de ce qui existe) |

### Exclu

- Activation réelle du recalibrage et du classifieur distillé : il n'y a pas encore de
  revues réelles. Seuls l'outillage et ses garde-fous sont livrés.
- Nouvelles langues et passage en couverture A d'un pays : ils exigent un sous-ensemble
  de référence étiqueté par l'équipe. Documente la procédure, n'active rien.
- Lots 7 (multi-plateforme) et 8 (médias) : reportés par Jules. Lot 9 : audit, plus tard.
- AFP (contrat commercial), Justel, Legilux et LégisQuébec (accès automatisé non
  vérifié) : seulement si leur documentation officielle confirme un accès ouvert.

## 4. Exigences propres au lot 6

- **RAM** : les synchronisations (LEGI, flux d'actualité) tournent dans un service
  hangar **dédié** (par exemple `atlas`), jamais dans tous les réplicas. XML analysé en
  flux, jamais chargé en entier. Aucun corpus en mémoire : recherche dans PostgreSQL.
  Rapporte le RSS de ce réplica au repos et pendant une synchronisation.
- **Sécurité XML** : flux et fichiers externes analysés sans résolution d'entités
  externes ; vérifie la protection contre l'expansion d'entités de la version d'expat
  utilisée, sinon utilise un parseur durci.
- **Coût** : chaque source passe par `source_cache` et le budget de son fournisseur ;
  les sources gratuites restent en tête de l'ordre de résolution, la recherche payante
  en dernier.
- **Neutralité** : aucune source ne change de niveau selon son orientation ; les flux
  d'actualité amorcés couvrent le service public, la presse nationale et régionale de
  sensibilités diverses et les agences ; la liste et son motif sont dans la
  documentation.
- **Droit d'auteur** (`connaissances.md`) : liens toujours conservés, passages cités
  seulement, pas d'article complet stocké hors besoin.
- **Doublures** : faux serveurs pour chaque nouvelle API dans les tests ; les appels
  réels sont faits seulement pour vérifier une API gratuite, à très bas volume. Dis
  dans le rapport ce qui a été appelé pour de vrai.

## 5. Méthode

1. Environnement et suite de départ (section 1), puis correctifs préalables.
2. Contrats : migration `0009` (expand seulement), valeurs du domaine, codes `TWR-ATL-*`
   et `TWR-FCK-*` à la suite des existants (ne jamais renuméroter), variables (dans
   `core/config.py`, `docs/configuration.md` et `.env.example`).
3. Implémentation par briques disjointes. Les adaptateurs statistiques, le droit,
   l'actualité et le registre se prêtent à des sous-agents Sonnet en parallèle si tu le
   juges utile (une base de test par sous-agent, fichiers disjoints, briefs précis).
4. Intégration : sélecteur de l'Atlas par juridiction, règle partie prenante, fiches de
   référence alimentées par les statistiques, isolation des imports (gate ne charge
   aucun adaptateur ; un hangar sans `atlas` ne charge rien de ses synchronisations).
5. Vérifications de fin de lot (`CLAUDE.md`), plus :
   - smoke test : une affirmation statistique européenne (Eurostat), une affirmation
     juridique française résolue par le miroir LEGI avec sa date de vigueur, une
     affirmation d'actualité trouvée dans `news_index`, avec les vrais processus,
     `TOWER_LLM_FAKE=1` et des faux serveurs si nécessaire ;
   - une synchronisation LEGI réelle d'au moins un code, si le téléchargement de la DILA
     est joignable (taille, durée, RSS) ;
   - `tower factcheck eval` sur les exemples, avec les nouvelles mesures.
6. Documentation française à jour dans le même commit que le comportement :
   - `fact-check.md` (nouvelle section « Lot 6 ») ;
   - `api.md`, `events.md`, `database.md`, `configuration.md`, `intercom.md`,
     `services.md` ;
   - `roadmap.md`, `factcheck/feuille-de-route.md`, `factcheck/connaissances.md` (ce qui
     est fait) ;
   - `decisions.md`, puis `docs/errors.md` régénéré.
7. Résumé `docs/sessions/AAAA-MM-JJ-lot-6-v3.md` (résultats réels, échecs, non
   vérifié), commits en anglais, push. Pas de nouvelle pull request sans demande.

## 6. Critères du lot (rappel)

≥ 70 % des affirmations tranchées sans recherche web payante ; calibration avec un
écart moyen < 0,1 ; distillation qui divise par deux les appels E2 sans perte de rappel
au-delà de 2 points. Sous doublures, ces critères ne sont **pas mesurables** : livre
l'outillage qui les mesure, montre-le sur les exemples, et dis-le clairement dans le
rapport.

## 7. Rapport final attendu

Court, un point par brique : fait, vérifié (comment), non vérifié (pourquoi), décisions
prises, ce que Jules doit faire (clés, liste des flux d'actualité à relire, passage des
codes prioritaires en production, base Railway).
