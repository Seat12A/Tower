# Cases

Un case porte sur un contenu X. Deux types partagent le même socle :

| `kind` | Usage |
|---|---|
| `fact_check` | Vérification d'affirmations (pipeline de fact-check à venir) |
| `moderation` | Modération de contenus X (service à venir) |

## Cycle de vie

```
received ──▶ in_review ──▶ validated ──▶ published
                 │
                 └──────▶ rejected
```

| Statut | Libellé | Permission requise pour y entrer |
|---|---|---|
| `received` | reçu | `cases:create` (ouverture) |
| `in_review` | en revue | `cases:review` (le vérificateur est assigné) |
| `validated` | validé | `cases:decide` |
| `rejected` | rejeté | `cases:decide` |
| `published` | publié | `cases:publish` + lien de la publication X |

Le graphe est défini **par type** dans `core/domain/cases.py` (`WORKFLOWS`). Pour
spécialiser la modération, il suffit de modifier son entrée.

## Règles

- **Aucune publication automatique** : l'équipe publie elle-même sur X, puis colle le
  lien (`POST /v1/cases/{id}/publish`, « Marquer comme publié » dans le bot). Le
  lien est validé (`https://x.com/<compte>/status/<id>`) et l'ID du post de
  publication est conservé dans l'audit.
- Transition interdite : `TWR-DOS-002` (409), avec la liste des transitions permises
  dans `details.allowed`.
- Concurrence : la ligne est verrouillée pendant la transition. Le client peut aussi
  envoyer `expected_version` ; en cas d'écart, `TWR-GEN-003` (409).
- Visibilité : le public ne voit que les cases `published`, et seulement les champs
  publics (pas d'assignation ni d'auteur interne). Un case non publié renvoie 404
  au public, pour ne pas révéler son existence.

## Audit et événements

Chaque ouverture ou transition écrit une ligne dans `case_events` et publie
`case.opened` ou `case.transitioned` dans la même transaction. Le payload
contient l'état complet du case (IDs en chaînes), plus `from`, `to` et `reason`.

## À venir

- Copie du post X à l'ouverture (`post_snapshots`), avec les tables d'ingestion.
- Lien entre cases et groupes d'affirmations (plusieurs posts par case).
