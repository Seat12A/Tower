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
    # --- brouillons de fact-check ----------------------------------------------------
    "error.TWR-FCK-007": "Brouillon introuvable. ({code})",
    "error.TWR-FCK-008": "Ce brouillon a déjà été tranché. ({code})",
    "error.TWR-FCK-009": (
        "Relecture invalide : une correction demande une véracité ou une note "
        "(280 caractères, liens exclus), un rejet demande une raison. ({code})"
    ),
    "error.TWR-FCK-012": (
        "Lien non pris en charge. Format attendu : https://x.com/<compte>/status/<id> ({code})"
    ),
    "veracity.true": "Vrai",
    "veracity.mostly_true": "Plutôt vrai",
    "veracity.misleading": "Trompeur",
    "veracity.false": "Faux",
    "veracity.unverifiable": "Non vérifiable",
    "context_need.none": "Aucun besoin",
    "context_need.useful": "Contexte utile",
    "context_need.necessary": "Contexte nécessaire",
    "band.high": "élevée",
    "band.medium": "moyenne",
    "band.low": "faible",
    "draft.title": "Brouillon de fact-check",
    "draft.header": (
        "**{veracity}** | {context_need} | Confiance {confidence} ({band}) | Niveau {level}{depth}"
    ),
    "draft.depth.reinforced": " | renforcée",
    "draft.claim.header": "**Affirmation**{tag}",
    "draft.claim.fr_tag": " (FR)",
    "draft.post_link": "Post : <{url}>",
    "draft.note.header": "**Note proposée** ({lang})",
    "draft.note.none": "Aucune note proposée.",
    "draft.rationale.header": "**Justification**",
    "draft.rationale.assertion": "- {assertion}",
    "draft.rationale.citation": "  - [{label}] « {quote} » ({source})",
    "draft.rationale.source": "{name} <{url}>",
    "draft.rationale.translation": "    Traduction : {text}",
    "draft.not_in_sources.header": "**Ce que les sources ne disent pas**",
    "draft.review_points.header": "**À vérifier en priorité**",
    "draft.objection": "> **OBJECTION** ({type}, {direction}) : {explanation}",
    "objection.type.misread_evidence": "preuve mal lue",
    "objection.type.insufficient_evidence": "preuves insuffisantes",
    "objection.type.not_independent": "sources non indépendantes",
    "objection.type.wrong_period_or_scope": "période ou périmètre erroné",
    "objection.type.different_definition": "définition différente",
    "objection.type.unfaithful_framing": "cadrage infidèle",
    "objection.direction.more_severe": "plus sévère",
    "objection.direction.less_severe": "moins sévère",
    "objection.direction.abstain": "abstention",
    "draft.warnings.header": "**Avertissements**",
    "draft.warning.downgraded": "- Verdict rétrogradé par la règle d'indépendance.",
    "draft.warning.post_deleted": "- Le post a été supprimé depuis son ingestion.",
    "draft.warning.post_modified": "- Le post a été modifié depuis son ingestion.",
    "draft.warning.budget_incomplete": "- Budget épuisé : la collecte est incomplète.",
    "draft.warning.cap_reached": "- Plafond de coût de l'enquête atteint : pas d'escalade.",
    "draft.warning.refused": "- Un modèle a refusé de répondre ({roles}).",
    "draft.warning.invalid_citations": (
        "- {count} citation(s) absente(s) mot pour mot de leur passage (écartée(s))."
    ),
    "draft.warning.loaded_terms": "- Termes chargés dans la note : {terms}.",
    "draft.warning.note_too_long": "- La note dépasse 280 caractères.",
    "draft.warning.unknown_links": (
        "- {count} lien(s) de la note ne sont pas des sources consultées."
    ),
    "draft.warning.sources_after_post": "- {count} source(s) postérieure(s) au post.",
    "draft.warning.sources_too_old": "- {count} source(s) trop ancienne(s) pour cette affirmation.",
    "draft.warning.conflicting_sources": "- Les sources se contredisent.",
    "draft.warning.critic_opinion": "- Critique : la note contient une opinion.",
    "draft.warning.critic_generalization": "- Critique : la note contient une généralisation.",
    "draft.warning.critic_judges_author": "- Critique : la note juge l'auteur.",
    "draft.warning.critic_loaded_words": "- Critique : mots chargés ({words}).",
    "draft.warning.critic_unsourced": "- Critique : {count} fait(s) sans source.",
    "draft.warning.critic_comment": "- Critique : {text}",
    "draft.button.validate": "Valider",
    "draft.button.correct": "Corriger",
    "draft.button.reject": "Rejeter",
    "draft.modal.correct.title": "Corriger le brouillon",
    "draft.modal.veracity.label": "Véracité (facultatif)",
    "draft.modal.veracity.placeholder": "{values}",
    "draft.modal.note.label": "Note (facultatif, 280 car. + liens)",
    "draft.modal.reason.label": "Raison (facultatif)",
    "draft.modal.reject.title": "Rejeter le brouillon",
    "draft.modal.reject.reason": "Raison (obligatoire)",
    "draft.bad_veracity": "Véracité inconnue `{value}`. Valeurs attendues : {values}.",
    "draft.review.endorsed": (
        "Votre approbation du brouillon `{short_id}` est enregistrée. "
        "Un éditeur doit maintenant le valider."
    ),
    "draft.review.validated": (
        "Brouillon `{short_id}` validé : le verdict est maintenant {veracity}."
    ),
    "draft.review.rejected": "Brouillon `{short_id}` rejeté.",
    "factcheck.accepted": (
        "Fact-check demandé pour le post {platform} `{external_id}`. "
        "Le brouillon apparaîtra dans le salon des dossiers."
    ),
    "factcheck.duplicate": ("Un fact-check du post {platform} `{external_id}` est déjà en file."),
    # --- profils de traitement et forçages de niveau -----------------------------------
    "error.TWR-FCK-029": "Version de profil introuvable. ({code})",
    "error.TWR-FCK-030": "Profil refusé : {message} ({code})",
    "error.TWR-FCK-031": "Forçage de niveau invalide : {message} ({code})",
    "error.TWR-FCK-032": "Cible du forçage introuvable. ({code})",
    "profile.show.header": "Profil actif : version {version} ({origin}).",
    "profile.show.default": "défaut issu des réglages",
    "profile.show.stored": "enregistré",
    "profile.show.general": (
        "Seuil même affirmation {same} | zone grise {grey} | quota de brouillons {quota}/jour | "
        "taux d'échantillonnage E1 {sample}"
    ),
    "profile.show.level": (
        "- {level} : seuil E1 {e1} | réutilisation directe {reuse} | plafond unitaire {cap} $ | "
        "budget mensuel {budget} $"
    ),
    "profile.yes": "oui",
    "profile.no": "non",
    "profile.history.header": "Versions du profil ({count}) :",
    "profile.history.line": "- v{version}{active} | {author} | {created} | {reason}",
    "profile.history.active": " (actif)",
    "profile.set.done": "Profil version {version} créé et activé : {path} = {value}.",
    "profile.rollback.done": (
        "Retour arrière : la version {version} du profil est de nouveau active."
    ),
    "level.set.done": (
        "Niveau {level} forcé sur {target_type} `{target}` jusqu'au {expires} "
        "(forçage `{short_id}`). {applied}"
    ),
    "level.set.applied": "Appliqué maintenant.",
    "level.set.not_applied": "Enregistré : pas encore appliqué à un groupe, ou différé.",
    "level.list.header": "Forçages de niveau actifs ({count}) :",
    "level.list.empty": "Aucun forçage de niveau actif.",
    "level.list.line": (
        "- `{short_id}` | {target_type} `{target}` -> {level} | jusqu'au {expires} | {reason}"
    ),
    "level.revoke.done": "Forçage `{short_id}` révoqué.",
    # --- groupes, faits de référence et bandes de valeur ---------------------------------
    "error.TWR-FCK-020": "Groupe d'affirmations introuvable. ({code})",
    "error.TWR-FCK-021": "Ce groupe a déjà été fusionné dans un autre groupe. ({code})",
    "error.TWR-FCK-022": (
        "Les deux groupes ont des verdicts différents : une fusion n'écrase jamais un verdict. "
        "({code})"
    ),
    "error.TWR-FCK-023": (
        "Seul le groupe qui absorbe l'autre peut porter le verdict actif. ({code})"
    ),
    "error.TWR-FCK-024": (
        "Scission invalide : les affirmations doivent être des membres actifs et le groupe "
        "ne peut pas être vidé. ({code})"
    ),
    "error.TWR-FCK-025": "Fait de référence introuvable. ({code})",
    "error.TWR-FCK-026": "Fait de référence invalide : {message} ({code})",
    "error.TWR-FCK-027": "Ce fait de référence n'est pas encore validé. ({code})",
    "error.TWR-FCK-028": "La bande de valeur ne peut pas être validée : {message} ({code})",
    "draft.group.line": (
        "**Groupe** `{group_id}` | {members} affirmation(s) | plateformes : {platforms}"
    ),
    "draft.group.other_posts": "Autres posts : {links}",
    "draft.group.related": (
        "Doublon possible : le groupe `{group_id}` a déjà un verdict ({veracity}). "
        "Utilisez « Même groupe que... » pour le fusionner."
    ),
    "draft.fact.line": (
        "**Référence** {label} : {value} {unit} (du {start} au {end}, {territory}, {status})"
    ),
    "draft.fact.band": "Bande : {label}",
    "draft.fact.outdated": "La valeur avancée correspond au chiffre précédent : périmée.",
    "draft.external.header": "**Fact-checks ailleurs**",
    "draft.external.line": "- {publisher} : {rating} <{url}>",
    "fact.status.pending": "en attente",
    "fact.status.validated": "validé",
    "fact.status.rejected": "rejeté",
    "band.kind.contains": "contient le fait",
    "band.kind.within_1_tau": "à moins de 1 tau du fait",
    "band.kind.within_3_tau": "à moins de 3 tau du fait",
    "band.kind.beyond": "au-delà de 3 tau du fait",
    "draft.button.samegroup": "Même groupe que...",
    "draft.button.split": "Scinder",
    "draft.button.fact": "Créer un fait",
    "draft.button.band": "Valider la bande",
    "draft.modal.samegroup.title": "Fusionner un autre groupe dans celui-ci",
    "draft.modal.samegroup.group": "Identifiant du groupe à absorber",
    "draft.modal.split.title": "Scinder des affirmations de ce groupe",
    "draft.modal.split.claims": "Identifiants d'affirmations (virgules)",
    "draft.modal.fact.title": "Créer un fait de référence",
    "draft.modal.fact.value": "Valeur",
    "draft.modal.fact.period": "Période (AAAA-MM-JJ AAAA-MM-JJ)",
    "draft.modal.fact.url": "URL de la source",
    "draft.modal.fact.name": "Nom de la source",
    "draft.modal.fact.published": "Publié le (AAAA-MM-JJ)",
    "group.merged": (
        "Fusion faite : le groupe `{group_id}` compte maintenant {members} affirmation(s)."
    ),
    "group.split": (
        "Scission faite : les affirmations sont dans le nouveau groupe `{group_id}` "
        "(pas encore de verdict)."
    ),
    "group.show.header": "Groupe `{group_id}` | {level} | {state} | {members} affirmation(s)",
    "group.show.merged_into": "Fusionné dans `{group_id}`.",
    "group.show.verdict": "Verdict : {veracity} (expire le {expires}, règle {rule})",
    "group.show.fact": "Fait : {label} = {value} {unit} | bande {band} ({side})",
    "group.show.member": "- `{claim_id}` {text} {links}",
    "group.show.event": "  journal : {kind} par {actor} le {when}",
    "fact.line": (
        "- `{short_id}` {status} | {label} : {value} {unit} | du {start} au {end} | {territory}"
    ),
    "fact.show.source": "Source : {source} <{url}> | publié le {published} | prochain {next}",
    "fact.list.header": "Faits de référence ({count}) :",
    "fact.list.empty": "Aucun fait de référence.",
    "fact.validated": "Fait `{short_id}` validé.",
    "fact.rejected": "Fait `{short_id}` rejeté.",
    "fact.created": "Fait `{short_id}` créé ({label}). Un relecteur doit le valider.",
    "fact.needs_validated": "Validez d'abord le verdict : un fait se crée depuis un verdict actif.",
    "band.validated": "Bande validée : {label} ({side}) -> {veracity}.",
}
