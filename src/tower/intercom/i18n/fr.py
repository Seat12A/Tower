"""Messages en français."""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # --- commun -----------------------------------------------------------------
    "common.more": "… et {count} de plus",
    "common.ambiguous": "Plusieurs résultats pour `{ref}` : soyez plus précis.",
    "common.none": "aucun",
    # --- erreurs (le code TWR est toujours affiché) -------------------------------
    "error.generic": "{message} ({code})",
    "error.TWR-BOT-001": (
        "L'API Tower est injoignable pour le moment. Réessayez dans un instant. ({code})"
    ),
    "error.TWR-AUTH-001": "Le jeton de service du bot est invalide ou révoqué. ({code})",
    "error.TWR-AUTH-002": "Le bot n'a pas pu vous identifier. ({code})",
    "error.TWR-AUTH-003": "Le bot n'a pas pu vous identifier. ({code})",
    "error.TWR-AUTH-004": "Vous n'êtes pas un membre actif de l'équipe. ({code})",
    "error.TWR-AUTH-005": "Vous n'avez pas la permission d'effectuer cette action. ({code})",
    "error.TWR-GEN-001": "Saisie invalide : {message} ({code})",
    "error.TWR-GEN-002": "Introuvable. ({code})",
    "error.TWR-GEN-003": "Quelqu'un a modifié cet élément entre-temps. Réessayez. ({code})",
    "error.TWR-GEN-004": "Trop de requêtes. Patientez un instant. ({code})",
    "error.TWR-DOS-001": "Dossier introuvable. ({code})",
    "error.TWR-DOS-002": (
        "Ce changement de statut n'est pas autorisé depuis le statut actuel. ({code})"
    ),
    "error.TWR-DOS-003": (
        "Lien X invalide. Format attendu : https://x.com/<compte>/status/<id> ({code})"
    ),
    "error.TWR-DOS-004": "Un lien de publication X est requis. ({code})",
    "error.TWR-ING-006": "Identifiant de plateforme invalide. ({code})",
    "error.TWR-ING-008": "Compte suivi introuvable. ({code})",
    "error.TWR-ING-009": "Ce compte est déjà dans la liste de suivi. ({code})",
    # --- statuts -------------------------------------------------------------------
    "status.received": "reçu",
    "status.in_review": "en revue",
    "status.validated": "validé",
    "status.published": "publié",
    "status.rejected": "rejeté",
    # --- liste de suivi ------------------------------------------------------------
    "watchlist.list.header": "Comptes suivis ({count}) :",
    "watchlist.list.empty": "Aucun compte suivi.",
    "watchlist.list.line": (
        "- {platform} {account} | {category} | priorité {priority} | `{short_id}`{pending}"
    ),
    "watchlist.pending": " | résolution en attente",
    "watchlist.added": (
        "{platform} {account} ajouté ({category}, priorité {priority}) sous `{short_id}`. "
        "La résolution de l'identifiant du compte par le radar est en attente."
    ),
    "watchlist.updated": "{platform} {account} mis à jour (`{short_id}`).",
    "watchlist.deactivated": "{platform} {account} désactivé (`{short_id}`).",
    "watchlist.nothing_to_change": "Rien à modifier : indiquez au moins un champ à changer.",
    # --- dossiers ------------------------------------------------------------------
    "dossier.show": (
        "Dossier `{public_id}`\n"
        "Statut : {status} | type : {kind} | priorité : {priority}\n"
        "Titre : {title}\n"
        "Post X : {x_post_id}\n"
        "Assigné à : {assigned_to}\n"
        "Publication : {published_url}\n"
        "Version : {version}"
    ),
    "dossier.transitioned": "Dossier `{short_id}` : {from_status} -> {to_status}.",
    "dossier.taken": "Dossier `{short_id}` pris en revue.",
    "dossier.validated": "Dossier `{short_id}` validé.",
    "dossier.rejected": "Dossier `{short_id}` rejeté.",
    "dossier.published": (
        "Lien de publication enregistré pour le dossier `{short_id}` : {url}\n"
        "Le bot ne publie jamais sur X : publiez le post vous-même."
    ),
    # --- incidents -----------------------------------------------------------------
    "incidents.list.header": "Incidents ouverts ({count}) :",
    "incidents.list.empty": "Aucun incident ouvert.",
    "incidents.list.line": "- {code} | {title} | x{count} | vu le {last_seen} | `{short_fp}`",
    "incidents.resolved": "Incident `{short_fp}` ({code}) résolu.",
    # --- consommation --------------------------------------------------------------
    "usage.header": "Consommation LLM (jour {day}, période du {period_start} au {period_end}) :",
    "usage.free.header": "Allocation gratuite du jour :",
    "usage.free.line": "- {provider}/{group} : {pct} % ({used} / {allowance} jetons)",
    "usage.free.unavailable": "- {provider}/{group} : aucune allocation",
    "usage.spend.header": "Dépenses de la période (USD) :",
    "usage.spend.line": "- {scope} : {spent} $ / {cap} $",
    "usage.spend.empty": "- aucun compteur pour l'instant",
    # --- notifications -------------------------------------------------------------
    "notify.incident.opened": "Incident ouvert {code} : {title} ({process}) `{short_fp}`",
    "notify.incident.reopened": "Incident rouvert {code} : {title} ({process}) `{short_fp}`",
    "notify.budget.threshold": "Budget {scope} : {threshold} % atteint ({spent} $ / {cap} $).",
    "notify.dossier.opened": "Nouveau dossier `{short_id}`{platform} : {title}",
    "notify.dossier.transitioned": "Dossier `{short_id}`{platform} : {from_status} -> {to_status}",
    "notify.watchlist.added": "Liste de suivi : {platform} {account} ajouté ({category}).",
    "notify.watchlist.updated": "Liste de suivi : {platform} {account} mis à jour.",
    "notify.watchlist.resolved": "Liste de suivi : {platform} {account} résolu.",
    "notify.watchlist.deactivated": "Liste de suivi : {platform} {account} désactivé.",
    "notify.extension_filters.published": "Filtres de l'extension publiés (version {version}).",
}
