"""English messages."""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # --- common -------------------------------------------------------------------
    "common.more": "... and {count} more",
    "common.ambiguous": "Several matches for `{ref}`: be more specific.",
    "common.none": "none",
    # --- errors (always show the TWR code) -----------------------------------------
    "error.generic": "{message} ({code})",
    "error.TWR-BOT-001": "The Tower API is unreachable right now. Try again in a moment. ({code})",
    "error.TWR-AUTH-001": "The bot's service token is invalid or revoked. ({code})",
    "error.TWR-AUTH-002": "The bot could not identify you. ({code})",
    "error.TWR-AUTH-003": "The bot could not identify you. ({code})",
    "error.TWR-AUTH-004": "You are not an active member of the team. ({code})",
    "error.TWR-AUTH-005": "You do not have the permission for this action. ({code})",
    "error.TWR-GEN-001": "Invalid input: {message} ({code})",
    "error.TWR-GEN-002": "Not found. ({code})",
    "error.TWR-GEN-003": "Someone modified this item meanwhile. Try again. ({code})",
    "error.TWR-GEN-004": "Too many requests. Wait a moment. ({code})",
    "error.TWR-DOS-001": "Dossier not found. ({code})",
    "error.TWR-DOS-002": "This status change is not allowed from the current status. ({code})",
    "error.TWR-DOS-003": "Invalid X link. Expected: https://x.com/<account>/status/<id> ({code})",
    "error.TWR-DOS-004": "An X publication link is required. ({code})",
    "error.TWR-ING-006": "Invalid platform identifier. ({code})",
    "error.TWR-ING-008": "Watched account not found. ({code})",
    "error.TWR-ING-009": "This account is already on the watchlist. ({code})",
    # --- statuses -------------------------------------------------------------------
    "status.received": "received",
    "status.in_review": "in review",
    "status.validated": "validated",
    "status.published": "published",
    "status.rejected": "rejected",
    # --- watchlist ------------------------------------------------------------------
    "watchlist.list.header": "Watched accounts ({count}):",
    "watchlist.list.empty": "No watched account.",
    "watchlist.list.line": (
        "- {platform} {account} | {category} | priority {priority} | `{short_id}`{pending}"
    ),
    "watchlist.pending": " | resolution pending",
    "watchlist.added": (
        "Added {platform} {account} ({category}, priority {priority}) as `{short_id}`. "
        "Resolution of the account id by the radar is pending."
    ),
    "watchlist.updated": "Updated {platform} {account} (`{short_id}`).",
    "watchlist.deactivated": "Deactivated {platform} {account} (`{short_id}`).",
    "watchlist.nothing_to_change": "Nothing to change: give at least one field to edit.",
    # --- dossiers -------------------------------------------------------------------
    "dossier.show": (
        "Dossier `{public_id}`\n"
        "Status: {status} | kind: {kind} | priority: {priority}\n"
        "Title: {title}\n"
        "X post: {x_post_id}\n"
        "Assigned to: {assigned_to}\n"
        "Publication: {published_url}\n"
        "Version: {version}"
    ),
    "dossier.transitioned": "Dossier `{short_id}`: {from_status} -> {to_status}.",
    "dossier.taken": "Dossier `{short_id}` taken into review.",
    "dossier.validated": "Dossier `{short_id}` validated.",
    "dossier.rejected": "Dossier `{short_id}` rejected.",
    "dossier.published": (
        "Publication link recorded for dossier `{short_id}`: {url}\n"
        "The bot never posts on X: publish the post manually."
    ),
    # --- incidents ------------------------------------------------------------------
    "incidents.list.header": "Open incidents ({count}):",
    "incidents.list.empty": "No open incident.",
    "incidents.list.line": "- {code} | {title} | x{count} | last seen {last_seen} | `{short_fp}`",
    "incidents.resolved": "Incident `{short_fp}` ({code}) resolved.",
    # --- usage ----------------------------------------------------------------------
    "usage.header": "LLM usage (day {day}, period {period_start} to {period_end}):",
    "usage.free.header": "Free allocation of the day:",
    "usage.free.line": "- {provider}/{group}: {pct}% ({used} / {allowance} tokens)",
    "usage.free.unavailable": "- {provider}/{group}: no allocation",
    "usage.spend.header": "Spend this period (USD):",
    "usage.spend.line": "- {scope}: ${spent} / ${cap}",
    "usage.spend.empty": "- no counter yet",
    # --- notifications --------------------------------------------------------------
    "notify.incident.opened": "Incident opened {code}: {title} ({process}) `{short_fp}`",
    "notify.incident.reopened": "Incident reopened {code}: {title} ({process}) `{short_fp}`",
    "notify.budget.threshold": "Budget {scope}: {threshold}% reached (${spent} / ${cap}).",
    "notify.dossier.opened": "New dossier `{short_id}`{platform}: {title}",
    "notify.dossier.transitioned": "Dossier `{short_id}`{platform}: {from_status} -> {to_status}",
    "notify.watchlist.added": "Watchlist: added {platform} {account} ({category}).",
    "notify.watchlist.updated": "Watchlist: updated {platform} {account}.",
    "notify.watchlist.resolved": "Watchlist: {platform} {account} resolved.",
    "notify.watchlist.deactivated": "Watchlist: deactivated {platform} {account}.",
    "notify.extension_filters.published": "Extension filters published (version {version}).",
}
