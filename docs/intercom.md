# intercom : le bot Discord

`tower intercom` est le bot Discord interne de l'équipe. C'est un **client de gate**
comme les autres : il n'accède jamais à la base. Il notifie les événements utiles dans
des salons et offre des commandes slash pour la liste de suivi, les dossiers, les
incidents et l'usage. Les permissions sont **toujours vérifiées par gate**, jamais
par le bot seul. Source : `src/tower/intercom/`.

## Briques

| Module | Rôle |
|---|---|
| `app.py` | Client discord.py, arbre de commandes, démarrage du notificateur. **Seul module qui importe `discord`** (importé paresseusement par la CLI) |
| `gate_client.py` | Client HTTP de gate : token de service, `X-Acting-Discord-Id` de la personne qui agit ; erreurs `GateError` (code `TWR-*` de gate) et `GateUnavailable` (`TWR-BOT-001`) ; lecture du flux SSE |
| `handlers/` | Logique des commandes, pure et indépendante de discord.py : `(gate, acting, locale, …) → Reply` |
| `i18n/` | Catalogues `fr` et `en` (mêmes clés, test de parité) |
| `notifier.py` | Événements SSE → messages courts dans les salons, reprise par curseur |

## Commandes

Toutes les réponses sont **éphémères** (visibles seulement par la personne) ; le bot
diffère la réponse (`defer`) puis répond, gate pouvant dépasser les 3 s de Discord. Une
erreur affiche le code `TWR-*` avec un message traduit. Les identifiants (`id` d'un
compte suivi, empreinte d'incident) acceptent un **préfixe unique d'au moins 6
caractères** ; pour un compte suivi, le bot relit la liste (500 au plus) pour obtenir la
`version` exigée par le PATCH (gate n'a pas de lecture unitaire d'un compte suivi).

| Commande | Effet | Permission gate |
|---|---|---|
| `/watchlist list [plateforme]` | Comptes suivis (pseudo, catégorie, priorité ; « en attente » tant que le radar n'a pas résolu le pseudo) | `watchlist:read` |
| `/watchlist add plateforme pseudo catégorie [priorité] [note]` | Ajout par pseudo ; le radar résout l'identifiant (voir [radar.md](radar.md)) | `watchlist:write` |
| `/watchlist edit id [catégorie] [priorité] [pseudo] [note]` | Modification (verrou optimiste géré par le bot) | `watchlist:write` |
| `/watchlist deactivate id` | Désactivation | `watchlist:write` |
| `/dossier show id` | État du dossier | `dossiers:read` |
| `/dossier take id` | Prise en revue (`in_review`) | `dossiers:review` |
| `/dossier validate id` / `reject id [note]` | Décision | `dossiers:decide` |
| `/dossier publish id lien` | Enregistre le lien du post publié ; **la publication sur X reste manuelle** | `dossiers:publish` |
| `/incidents list` / `resolve empreinte [note]` | Erreurs stockées | `errors:read` / `errors:manage` |
| `/usage` | Allocation gratuite du jour, dépense du mois par portée (`global`, fournisseurs, `provider:x`) | `usage:read` |
| `/factcheck url` | Demande de fact-check manuelle (N4) d'un post X ; répond « acceptée » ou « déjà demandée » | `factcheck:request` |
| `/group show id` | Groupe : membres, posts, verdict, fiche et bande, journal | `dossiers:read` |
| `/group merge cible source motif` / `/group split id affirmations motif` | Fusion et scission (réversibles) | `claim_groups:manage` |
| `/fact show id` / `/fact list [statut]` | Fiches de référence | `dossiers:read` |
| `/fact validate id` / `/fact reject id motif` | Validation d'une fiche | `reference_facts:validate` |
| `/fact band verdict` | Valide la bande d'un verdict validé | `reference_facts:validate` |
| `/profile show` / `/profile history` | Profil actif, versions | `profiles:read` |
| `/profile set chemin valeur motif` / `/profile rollback motif` | Nouvelle version activée / retour à la précédente (bornes de sécurité) | `profiles:manage` (admin) |
| `/level set type cible niveau heures motif` / `/level list` / `/level revoke id motif` | Forçage de niveau d'un contenu, d'un groupe ou d'un compte | `levels:override` |

`/incidents` demande le rôle `admin` (seul rôle avec `errors:*`). Les droits effectifs sont l'intersection des rôles de la personne et des scopes du
token du bot (voir [auth.md](auth.md)). Token du bot : `dossiers:read
dossiers:create dossiers:review dossiers:decide dossiers:publish events:read
errors:read errors:manage watchlist:read watchlist:write usage:read
factcheck:request claim_groups:manage reference_facts:write reference_facts:validate
profiles:read profiles:manage levels:override` (lot 4 : à ajouter au token existant).

## Notifications

| Topic | Salon |
|---|---|
| `incident.opened`, `incident.reopened` | `TOWER_DISCORD_CHANNEL_INCIDENTS` |
| `budget.threshold_reached` | `TOWER_DISCORD_CHANNEL_BUDGET` |
| `dossier.opened`, `dossier.transitioned` | `TOWER_DISCORD_CHANNEL_DOSSIERS` |
| `draft.ready` (brouillon de fact-check complet, avec boutons) | `TOWER_DISCORD_CHANNEL_DOSSIERS` |
| `watchlist.changed`, `extension_filters.published` | `TOWER_DISCORD_CHANNEL_ADMIN` |

Messages courts, avec la plateforme d'origine quand l'événement en porte une, dans la
langue `TOWER_DISCORD_LOCALE` (`fr` par défaut). Salon non configuré : notification
ignorée. Aucun événement à haut volume : le bot ne s'abonne pas à `content.ingested`.

### Brouillons de fact-check (E11)

Sur `draft.ready`, le bot lit `GET /v1/drafts/{id}` et publie le brouillon (rendu pur
dans `handlers/factcheck.py`, découpé sous les limites de Discord sans couper une URL) :
en-tête (issue, besoin de contexte, confiance et bande, niveau), affirmation et lien du
post, note proposée, justification avec citations (dans leur langue d'origine, avec la
traduction française du passage pour les notifications en français) et liens, ce que
les sources ne disent pas, points à vérifier (objection du second avis mise en avant),
avertissements (rétrogradation, post supprimé ou modifié, budget incomplet, refus de
modèle, citations invalides, mots chargés, remarques du critique). Puis il appelle
`POST /v1/drafts/{id}/notified`.

Boutons persistants (`fc:<action>:<id>`, `DynamicItem` enregistré au démarrage, donc
valables après un redémarrage) :

| Bouton | Effet |
|---|---|
| Valider | `review` `accept` |
| Corriger | Fenêtre de saisie : issue (une des cinq valeurs, facultative), texte de la note (≤ 280 caractères hors liens, facultatif), motif → `review` `edit` ; la note corrigée est enregistrée dans la langue Discord de la personne (fr ou en) |
| Rejeter | Fenêtre de saisie, motif obligatoire → `review` `reject` |

Les droits suivent [auth.md](auth.md) : un `reviewer` qui valide ou corrige enregistre un
avis favorable, un `editor` valide le verdict. Les boutons disparaissent une fois le
brouillon validé ou rejeté. Réponses éphémères, dans la langue Discord de la personne.

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
