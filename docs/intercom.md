# intercom : le bot Discord

`tower intercom` est le bot Discord interne de l'équipe. C'est un **client de gate**
comme les autres : il n'accède jamais à la base. Il notifie les événements utiles dans
des salons et offre des commandes slash. Les permissions sont **toujours vérifiées par
gate** ; le bot ajoute seulement la règle de hiérarchie des rôles Discord (voir
[Équipe et permissions](#équipe-et-permissions)). Source : `src/tower/intercom/`.

L'interface suit le kit de design de Moddy : [design/DESIGN.md](design/DESIGN.md)
(règles), [design/COMPONENTS_V2.md](design/COMPONENTS_V2.md),
[design/MODALS_V2.md](design/MODALS_V2.md), [design/PERSISTENT_VIEWS.md](design/PERSISTENT_VIEWS.md).
En bref : **Components V2 uniquement** (aucun embed, aucun message texte brut),
**émojis perso uniquement** (`ui/emojis.py`, jamais un émoji Unicode), **composants
persistants uniquement** (chaque bouton ou menu marche encore après un redémarrage),
**modals V2** quand c'est plus pratique qu'une option slash (motifs, formulaires,
choix contraints).

## Briques

| Module | Rôle |
|---|---|
| `app.py` | Client discord.py : arbre de commandes, enregistrement des composants persistants, démarrage du notificateur |
| `gate_client.py` | Client HTTP de gate : token de service, `X-Acting-Discord-Id` de la personne qui agit ; erreurs `GateError` (code `TWR-*` de gate) et `GateUnavailable` (`TWR-BOT-001`) ; lecture du flux SSE |
| `handlers/` | Logique des commandes, **pure** (sans discord.py) : `(gate, acting, locale, …) → Reply`. Un `Reply` décrit une carte : titre et icône, accent, corps, détails, éléments cités, champs, pied, boutons |
| `ui/emojis.py` | Toutes les icônes (émojis d'application copiés de Moddy ; correspondance des ID : `docs/design/emoji_mapping.json`) |
| `ui/render.py` | `Reply` → `LayoutView` V2 (anatomie de la carte, limites de 4000 caractères et 40 composants), envoi, édition, publication |
| `ui/base.py` | `run_handler` (diffère, appelle le handler, affiche la carte ; panneau : édition sur place), `guarded_callback`, `BaseModal`, carte de bug `TWR-INT-001` |
| `commands/` | Un module par domaine : `register(tree, ctx)`, ses `DynamicItem` et un `custom_id` d'exemple par item (`SAMPLE_CUSTOM_IDS`) |
| `delivery.py` | Envoi des cartes de notification et des brouillons dans les salons (mode dry-run : journal seulement) |
| `i18n/` | Catalogues `fr` et `en` : une base plus un module par domaine (`ops`, `fc`, `team`), mêmes clés et paramètres (test de parité). Le français vouvoie |
| `notifier.py` | Événements SSE → cartes dans les salons, reprise par curseur |

`discord` n'est importé que par le processus du bot (`app.py`, `commands/`, `ui/render.py`,
`ui/base.py`, `delivery.py`) ; `handlers/`, `notifier.py` et `gate_client.py` restent sans
discord (test d'isolation).

### Composants persistants

Chaque bouton ou menu porte un `custom_id` `t12a:<domaine>:<action>[:<paramètres>]`
(100 caractères au plus) reconnu par **un seul** `DynamicItem`, enregistré au démarrage.
Le clic reconstruit l'item depuis le `custom_id`, relit les données dans gate et met la
carte à jour sur place ; rien n'est gardé en mémoire. Une erreur sur un panneau part en
message privé, le panneau reste tel quel. Les anciens boutons de brouillon (`fc:…`)
restent acceptés. Tests : `tests/test_intercom_ui.py` (gabarits disjoints, budget V2) et
`tests/intercom_ui.py` (`assert_persistent`).

## Commandes

Toutes les commandes sont réservées au serveur et toutes les réponses sont **éphémères**.
Le bot diffère la réponse puis affiche la carte (gate peut dépasser les 3 s de Discord).
Une erreur attendue donne une carte calme (titre court, quoi faire, code `TWR-*` en pied) ;
un bug donne une carte rouge foncé `TWR-INT-001`. Les identifiants acceptent un **préfixe
unique d'au moins 6 caractères**.

| Commande | Forme | Permission gate |
|---|---|---|
| `/help` | Commandes utilisables par la personne, par domaine | membre de l'équipe |
| `/team` | Membres rangés (admins en tête) ; menu pour ouvrir les permissions d'un membre | `staff:read` |
| `/rank membre` | Range un membre ; bouton « Gérer les permissions » | `staff:manage` |
| `/unrank membre` | Confirmation sur place, puis retrait du rang et de toutes les permissions | `staff:manage` |
| `/permissions [membre]` | Panneau : un menu à choix multiples par catégorie, enregistrement immédiat ; « Tout retirer » avec confirmation | `permissions:manage_self` (soi) ou `permissions:manage_others` ; voir un autre membre : `staff:read` |
| `/watchlist list [plateforme]` | Liste paginée (8 par page), filtre de plateforme, menu « gérer un compte » (carte : modifier, désactiver), bouton « Ajouter » | `watchlist:read` |
| `/watchlist add` | Modal : plateforme et catégorie (menus), pseudo, priorité, note ; le radar résout l'identifiant ([radar.md](radar.md)) | `watchlist:write` |
| `/watchlist edit id` / `deactivate id` | Modal (champ vide = inchangé ; verrou optimiste géré par le bot) / confirmation sur place | `watchlist:write` |
| `/dossier show id` | Carte avec Prendre, Valider, Rejeter (modal), Publier (modal : lien) selon le statut, lien vers le post | `dossiers:read` (+ actions) |
| `/dossier take` / `validate` / `reject` / `publish id` | Mêmes actions en commande ; **la publication sur X reste manuelle** | `dossiers:review` / `dossiers:decide` / `dossiers:publish` |
| `/incidents list [code] [all]` | Liste (filtre par code `TWR-…`, `all` : résolus compris), menus « voir » et « résoudre » (modal : note), actualisation | `errors:read` |
| `/incidents show empreinte` | Détail (préfixe de 6 caractères accepté, résolus compris) : statut, sévérité, occurrences, première et dernière occurrence, processus, emplacement, exception, relançable, note de résolution, dernier message, piste du catalogue, dernier contexte (12 clés au plus) et fin de la dernière trace, le tout borné à un message Discord ; boutons « Résoudre » (si ouvert) et retour à la liste | `errors:read` (`errors:manage` pour résoudre) |
| `/incidents resolve empreinte` | Modal : note facultative | `errors:manage` |
| `/usage` | Allocation gratuite du jour, dépense du mois par portée ; actualisation | `usage:read` |
| `/factcheck url` | Demande de fact-check manuelle (N4) : « demandée » ou « déjà demandée » | `factcheck:request` |
| `/group show id` | Carte du groupe : actualiser, fusionner ici (modal), scinder (modal) | `dossiers:read`, `claim_groups:manage` |
| `/group merge` / `split` | Fusion et scission (réversibles) | `claim_groups:manage` |
| `/fact show id` / `list [statut]` | Fiche (valider, rejeter avec modal) / liste avec menu d'ouverture | `dossiers:read`, `reference_facts:validate` |
| `/fact validate` / `reject` / `band` | Validation d'une fiche ou de la bande d'un verdict | `reference_facts:validate` |
| `/profile show` / `history` | Profil actif (boutons historique, modifier, revenir) / versions | `profiles:read` |
| `/profile set` / `rollback` | Modal (chemin, valeur, motif) / modal (motif) ; bornes de sécurité | `profiles:manage` |
| `/level set` / `list` / `revoke id` | Modal (type de cible en choix unique, cible, niveau, heures, motif) / liste avec menu « révoquer » / révocation (modal : motif) | `levels:override` |
| `/narrative list [état]` / `show id` | Liste avec menu / carte (accent selon l'état) : actualiser, valider le titre, corriger le titre, fusionner, scinder (modals) | `dossiers:read` (+ actions) |
| `/narrative merge` / `split` / `title` | Fusion, scission, titre | `claim_groups:manage` / `dossiers:review` |
| `/registry show domaine` | Carte : niveau, contrôle (modals, motif obligatoire), historique, amorçage Wikidata | `registry:read` / `registry:write` |
| `/registry history` / `set-tier` / `set-control` / `seed-control domaine` | Versions / changement de niveau / relation de contrôle (`control_origin = team`) / amorçage | `registry:read` / `registry:write` |

Les droits effectifs sont l'intersection des permissions de la personne et des scopes
du token du bot ([auth.md](auth.md)). Token du bot : `dossiers:read dossiers:create
dossiers:review dossiers:decide dossiers:publish events:read errors:read errors:manage
watchlist:read watchlist:write usage:read factcheck:request claim_groups:manage
reference_facts:write reference_facts:validate profiles:read profiles:manage
levels:override registry:read registry:write staff:read staff:manage
permissions:manage_others permissions:manage_self` (les quatre derniers : à ajouter au
token existant).

### Équipe et permissions

- **Admins codés en dur** (`TOWER_ADMIN_DISCORD_IDS`, lu par gate) : toutes les
  permissions, rangés d'office, ni modifiables ni retirables.
- **Seuls les membres rangés** (`/rank`) reçoivent des permissions ; `/unrank` retire tout.
- Permissions détaillées par catégorie (Dossiers, Fact-check, Liste de suivi, Registre,
  Profils, Supervision, Équipe) avec une description par permission (`handlers/team.py`).
- Modifier les permissions des autres (`permissions:manage_others`) et les siennes
  (`permissions:manage_self`) sont deux permissions distinctes. Un non-admin ne donne ou ne
  retire que des permissions qu'il possède (gate, `TWR-STF-006`).
- **Hiérarchie Discord** (vérifiée par le bot, gate ne connaît pas les rôles) : hors
  admins, on n'agit (rang, retrait, permissions) que sur un membre dont le rôle le plus
  haut est **strictement inférieur** au sien ; un non-admin n'agit jamais sur un admin. Le
  bot lit le membre ciblé par REST (`guild.fetch_member`), sans intent `members`.

## Notifications

| Topic | Salon | Carte (accent) |
|---|---|---|
| `incident.opened`, `incident.reopened` | `TOWER_DISCORD_CHANNEL_INCIDENTS` | rouge |
| `budget.threshold_reached` | `TOWER_DISCORD_CHANNEL_BUDGET` | jaune, rouge à 100 % |
| `dossier.opened`, `dossier.transitioned`, `draft.ready`, `draft.reviewed` | Forum `TOWER_DISCORD_FORUM_DOSSIERS` (un post par dossier, voir ci-dessous) ; repli sans forum : salon texte `TOWER_DISCORD_CHANNEL_DOSSIERS` | voir ci-dessous |
| `narrative.surge_detected` (palier `surge` ; palier `alert` marqué « ALERTE VAGUE », précédé de la mention du rôle `TOWER_DISCORD_ALERT_ROLE_ID` s'il est configuré), `narrative.surge_ended` (bilan) | `TOWER_DISCORD_CHANNEL_NARRATIVES` (à défaut, salon texte des dossiers sans forum) | jaune (`surge`), rouge (`alert`), bleu (fin) |
| `watchlist.changed`, `extension_filters.published` | `TOWER_DISCORD_CHANNEL_ADMIN` | bleu |

Cartes V2 publiques (titre avec icône, corps, détails), avec la plateforme d'origine quand l'événement en porte une, **toujours en
anglais** : les lecteurs d'un salon ou d'un forum sont inconnus (`TOWER_DISCORD_LOCALE`
est dépréciée et ignorée). Le texte produit par les modèles (justification, critique,
points à vérifier) reste dans sa langue (le pipeline l'écrit en français), marqué `(FR)`
quand il s'agit de l'affirmation. Salon non configuré : notification
ignorée. Aucun événement à haut volume : le bot ne s'abonne pas à `content.ingested`.

### Brouillons de fact-check (E11)

Sur `draft.ready`, le bot lit `GET /v1/drafts/{id}` et publie le brouillon en cartes V2
(rendu pur dans `handlers/factcheck.py`, découpé sous les limites V2 sans couper une URL,
10 cartes au plus ; accent jaune s'il y a une objection ou des avertissements) :
en-tête (issue, besoin de contexte, confiance et bande, niveau), affirmation et lien du
post, note proposée, justification avec citations (dans leur langue d'origine, avec la
traduction française du passage pour les notifications en français) et liens, ce que
les sources ne disent pas, points à vérifier (objection du second avis mise en avant),
avertissements (rétrogradation, post supprimé ou modifié, budget incomplet, refus de
modèle, citations invalides, mots chargés, remarques du critique). Puis il appelle
`POST /v1/drafts/{id}/notified`.

Boutons persistants sur la dernière carte (`t12a:fc:<action>:<id>` ; les anciens
`fc:<action>:<id>` restent acceptés). Chaque clic relit le brouillon dans gate :

| Bouton | Effet |
|---|---|
| Valider | `review` `accept` |
| Corriger | Modal V2 : issue (choix unique pré-rempli), texte de la note (≤ 280 caractères hors liens, facultatif), motif → `review` `edit` ; la note corrigée est enregistrée dans la langue Discord de la personne (fr ou en) |
| Rejeter | Modal, motif obligatoire → `review` `reject` |

Les droits suivent [auth.md](auth.md) : un `reviewer` qui valide ou corrige enregistre un
avis favorable, un `editor` valide le verdict. Une fois le brouillon décidé, le message
est édité : ligne grise d'issue, accent recoloré ; un brouillon rejeté perd tous ses
boutons, un brouillon validé garde « Même groupe », « Scinder », « Créer une fiche » et
« Valider la bande » (ces deux derniers exigent un verdict validé). Un clic sur un
brouillon déjà décidé affiche « déjà décidé » et retire les boutons. Réponses éphémères, dans la langue Discord de la personne.

**Réconciliation au démarrage** : avant de lire le flux, le bot affiche les brouillons en
attente non encore affichés (`GET /v1/drafts?status=pending&notified=false`, paginé). Cela
couvre aussi la limite du notificateur (événements manqués au tout premier démarrage).
Un `draft.ready` rejoué pour un brouillon déjà affiché ou déjà décidé est ignoré.

Lot 4 : le message du brouillon montre aussi le **groupe** (nombre d'affirmations,
autres posts et plateformes, groupe proche suggéré et son verdict), la **fiche et la
bande** d'une affirmation chiffrée (avec l'alerte « périmé »), et les fact-checks
externes trouvés. Boutons ajoutés : « Même groupe que… » (fenêtre : identifiant d'un
groupe, fusionné dans celui du brouillon), « Scinder » (affirmations et motif), « Créer
une fiche » (valeur, période, source ; seulement une fois le verdict validé), « Valider
la bande » (éditeur). Les erreurs `TWR-FCK-020` à `030` sont traduites.

Lot 5 : le message du brouillon montre le **récit** du groupe (titre, état) et un bloc
**signaux de coordination** (« informatif, jamais utilisé pour le verdict »). Les
notifications de vague sont idempotentes par récit et palier (une vague qui monte de
`surge` à `alert` est notifiée deux fois ; une fin de vague libère le récit pour une
vague ultérieure ; mémoire bornée). Seule l'alerte de vague mentionne un rôle :
`TOWER_DISCORD_ALERT_ROLE_ID` (mention en tête de carte, `AllowedMentions` limité à ce
rôle) ; tout le reste part sans mention. Les cartes de vague portent un bouton
`t12a:nar:open:<id>` qui ouvre le récit en message privé. Les erreurs
`TWR-FCK-043` et `044` sont traduites.

### Forum des dossiers

Avec `TOWER_DISCORD_FORUM_DOSSIERS` (salon de type forum), **un post = un dossier**
(`intercom/forum.py`, rendu pur dans `handlers/forum.py`, côté Discord
`delivery.ForumDelivery`). Le lien dossier ↔ post est enregistré dans gate
(`PUT /v1/dossiers/{id}/discord-thread`), jamais en mémoire : après un redémarrage, le bot
retrouve le post d'un dossier par `discord_thread_id`.

**Le message d'origine est le seul panneau de contrôle** (édité à chaque changement) :
titre = statut et niveau ; l'affirmation ; verdict proposé, besoin de contexte,
confiance ; la note proposée avec sa longueur (`n/280`) ; le **nombre**
d'avertissements et l'objection éventuelle (le détail est dans le fil) ; une barre
d'actions (Validate, Correct, Reject, Open post) ; un seul menu **More actions**
(`t12a:fc:more:<id>` : Same group as…, Split, Create a reference fact, Validate the band,
Show the group, Show the narrative). Une fois validé : Mark as published (modal du lien X)
+ menu réduit (fiche, bande, groupe, récit) ; rejeté ou publié : lien du post + menu
d'information. Le bot n'édite jamais un post au clic : il le met à jour d'après les
événements de la décision (`draft.reviewed`, `dossier.transitioned`).

**Le fil**, toujours dans cet ordre, chaque section omise si vide et découpée en
« (cont.) » si elle dépasse la limite V2 : *Evidence* (affirmations de la justification,
citations `[P1]` et renvoi `[S1]` ; les sources sont listées **une seule fois** en fin de
carte), *Gaps & checks* (ce que les sources ne disent pas, points à vérifier, objection en
tête), *Quality warnings* (contrôles puis critique), *Context* (groupe, récit, signaux de
coordination, fiche et bande, fact-checks externes). Puis une ligne par action (avis,
validation, correction, rejet avec motif, publication ; transitions des dossiers sans
brouillon), avec la mention de la personne sans notification. Le fil sert aussi à la
discussion.

**Tags** (18, `moderated` : seul le bot les pose ; créés au démarrage s'ils manquent, par
nom, jamais supprimés) : un statut (*To review*, *Endorsed*, *Validated*, *Published*,
*Rejected*, *Superseded*), l'issue proposée, le niveau (*N1* à *N4*) et au plus deux
signaux (*Needs attention* : avertissements ou objection ; *Wave* : récit en vague ;
*Post deleted*). Au plus 5 par post (limite Discord). Le bot règle aussi le tri par date
de création, la vue liste, et épingle un post « How to review » (légende).

**Cycle de vie** : `dossier.opened` crée le post (carte courte) ; `draft.ready` le
complète (carte entière, tags, sections) puis appelle `notified` ; un post supprimé à la
main est recréé ; `published`, `rejected` et `superseded` **archivent** le post (sans le
verrouiller : on peut encore écrire), pour rester sous la limite de fils actifs du serveur.
Un événement à la fois : les posts sont créés l'un après l'autre (contre-pression). Un
échec est journalisé (`TWR-BOT-003`) ; la réconciliation du démarrage affiche les
brouillons jamais affichés.

**Icônes** : tout ou rien par type d'élément (barre, options du menu, titres de section,
titres de statut, lignes du journal). Les icônes manquantes sont déclarées vides dans
`ui/emojis.py` (`MERGE`, `SPLIT`, `BAND`, `NARRATIVE`, `EVIDENCE`, `CHECKLIST`, `CONTEXT`,
`ENDORSED`, `PUBLISH`, `WAVE`) : tant qu'une icône d'un type manque, ce type s'affiche sans
icône. Les tags n'ont pas d'icône.

Permissions du bot sur le forum : Gérer le salon (tags, réglages), Gérer les fils (tags
modérés, archivage, épinglage), Envoyer des messages dans les fils.

### Reprise sans perte

Une seule connexion SSE (`GET /v1/events?after=…&topics=…`). Le curseur est stocké côté
gate (`PUT /v1/events/cursors/intercom`, voir [events.md](events.md)) après chaque
événement traité ; au redémarrage, le bot le relit et reprend avec `?after=`. Premier
démarrage sans curseur : départ à « maintenant » (pas d'inondation du salon) ; limite
connue : si le flux se coupe avant le tout premier événement, la reprise repart de
« maintenant » et les événements de cet intervalle sont manqués (une seule fois, au
tout premier démarrage). Livraison
**au moins une fois** : les identifiants d'événements déjà notifiés sont mémorisés dans
un ensemble borné (2 048) ; un doublon n'est pas renvoyé. Flux coupé : `TWR-BOT-002`
journalisé, reprise avec recul (1 à 30 s) depuis le dernier curseur. Échec d'envoi
Discord : `TWR-BOT-003` journalisé, la boucle continue. Deux réplicas ne cassent rien
(doublons possibles mais pas de perte) ; un seul est prévu.

## Langues

Chaque texte vient du catalogue `fr` ou `en` choisi d'après la langue Discord de
l'utilisateur (`interaction.locale` : `fr`, `fr-FR`… → français ; sinon anglais). Clés
et code en anglais. Un test vérifie que les deux catalogues ont exactement les mêmes
clés et les mêmes paramètres.

## RAM

Cible : au plus 120 Mo de RSS au repos. Intents minimaux (`guilds` seulement : ni
contenu des messages ni membres), `member_cache_flags` à aucun,
`chunk_guilds_at_startup=False`, `max_messages=None` (pas de cache de messages), une
seule connexion SSE, ensemble de déduplication borné. gate et hangar ne chargent
jamais `discord` (test d'isolation).

## Lancer

```bash
uv run tower intercom             # TOWER_DISCORD_TOKEN, TOWER_GATE_URL, TOWER_GATE_TOKEN
```

```bash
uv run tower intercom --dry-run   # construit tout sans se connecter à Discord ; notifications journalisées
```

Configuration manquante ou token Discord refusé : `TWR-BOT-004` et arrêt (code 2). Variables :
[configuration.md](configuration.md#intercom-bot-discord). Déploiement :
[deployment-railway.md](deployment-railway.md).
