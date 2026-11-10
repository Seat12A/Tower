# Catalogue des codes d'erreur

> Page générée par `tower errors catalog --markdown > docs/errors.md`.
> Ne pas éditer à la main : la source est `src/tower/core/domain/error_codes.py`.

Format : `TWR-<DOMAINE>-<NNN>`. Un code n'est jamais renuméroté ni réutilisé.
« Stocké » : l'occurrence est agrégée dans la table `error_reports`.

## AUTH : Authentification et autorisations

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-AUTH-001` | Token de service invalide ou révoqué | 401 | warning | non | non |  |
| `TWR-AUTH-002` | X-Acting-Discord-Id fourni sans token de service | 401 | warning | non | non |  |
| `TWR-AUTH-003` | X-Acting-Discord-Id invalide | 401 | warning | non | non |  |
| `TWR-AUTH-004` | Membre de l'équipe inconnu ou inactif | 403 | warning | non | non | Ajouter le membre : tower staff <discord_id> --roles reviewer |
| `TWR-AUTH-005` | Permission manquante | 403 | info | non | non |  |

## DB : Base de données

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-DB-001` | Base de données indisponible | 503 | critical | oui | oui |  |
| `TWR-DB-002` | Connexion LISTEN perdue (reconnexion en cours) | 500 | warning | oui | oui |  |

## DOS : Dossiers

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-DOS-001` | Dossier introuvable | 404 | info | non | non |  |
| `TWR-DOS-002` | Transition de statut interdite | 409 | info | non | non |  |
| `TWR-DOS-003` | Lien de publication X invalide | 422 | info | non | non | Format attendu : https://x.com/<compte>/status/<id> |
| `TWR-DOS-004` | Lien de publication X obligatoire | 422 | info | non | non |  |
| `TWR-DOS-005` | Lien de publication accepté uniquement pour le statut publié | 422 | info | non | non |  |
| `TWR-DOS-006` | Priorité hors bornes (-100..100) | 422 | info | non | non |  |

## EVT : Bus d'événements

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-EVT-001` | Curseur d'événements invalide | 422 | info | non | non |  |
| `TWR-EVT-002` | Échec d'un consommateur d'événements (lot rejoué) | 500 | error | oui | oui | Le curseur n'avance pas tant que le lot échoue : corriger le handler. |

## GEN : Général

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-GEN-001` | Entrée invalide | 422 | info | non | non |  |
| `TWR-GEN-002` | Ressource introuvable | 404 | info | non | non |  |
| `TWR-GEN-003` | Modification concurrente (version obsolète) | 409 | info | non | non | Relire la ressource puis renvoyer avec la nouvelle version. |
| `TWR-GEN-004` | Trop de requêtes | 429 | info | non | non |  |

## INT : Interne

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-INT-001` | Erreur interne inattendue | 500 | error | oui | non | Chercher l'empreinte dans error_reports ou request_id dans les logs Railway. |
| `TWR-INT-002` | Tâche de fond d'un processus arrêtée sur erreur | 500 | critical | oui | non |  |

## JOB : File de travaux

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-JOB-001` | Aucun handler pour ce type de job | 500 | error | oui | non | Le service n'est pas activé dans TOWER_SERVICES de ce réplica. |
| `TWR-JOB-002` | Job en échec après la dernière tentative | 500 | error | oui | non |  |
| `TWR-JOB-003` | Job en échec définitif (PermanentError) | 500 | error | oui | non |  |
| `TWR-JOB-004` | Bail expiré : worker perdu (OOM, arrêt brutal) | 500 | warning | oui | oui | Vérifier la mémoire du réplica et drainingSeconds sur Railway. |
| `TWR-JOB-005` | Job interrompu : délai d'exécution dépassé | 500 | warning | oui | oui |  |
| `TWR-JOB-006` | Tentative de job échouée, relance programmée | 500 | warning | oui | oui |  |
| `TWR-JOB-007` | Impossible d'enregistrer le résultat d'un job | 500 | error | oui | non | Le reaper remettra le job en file à l'expiration du bail. |

## MNT : Maintenance

| Code | Titre | HTTP | Sévérité | Stocké | Relançable | Piste |
|---|---|---|---|---|---|---|
| `TWR-MNT-001` | Tâche de maintenance en échec | 500 | error | oui | non |  |

