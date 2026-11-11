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
    # --- fact-check drafts ----------------------------------------------------------
    "error.TWR-FCK-007": "Draft not found. ({code})",
    "error.TWR-FCK-008": "This draft was already decided. ({code})",
    "error.TWR-FCK-009": (
        "Invalid review: an edit needs a veracity or a note (280 characters, links excluded), "
        "a rejection needs a reason. ({code})"
    ),
    "error.TWR-FCK-012": (
        "Unsupported link. Expected format: https://x.com/<account>/status/<id> ({code})"
    ),
    "veracity.true": "True",
    "veracity.mostly_true": "Mostly true",
    "veracity.misleading": "Misleading",
    "veracity.false": "False",
    "veracity.unverifiable": "Unverifiable",
    "context_need.none": "No context needed",
    "context_need.useful": "Context useful",
    "context_need.necessary": "Context necessary",
    "band.high": "high",
    "band.medium": "medium",
    "band.low": "low",
    "draft.title": "Fact-check draft",
    "draft.header": (
        "**{veracity}** | {context_need} | Confidence {confidence} ({band}) | Level {level}{depth}"
    ),
    "draft.depth.reinforced": " | reinforced",
    "draft.claim.header": "**Claim**{tag}",
    "draft.claim.fr_tag": " (FR)",
    "draft.post_link": "Post: <{url}>",
    "draft.note.header": "**Proposed note** ({lang})",
    "draft.note.none": "No note proposed.",
    "draft.rationale.header": "**Rationale**",
    "draft.rationale.assertion": "- {assertion}",
    "draft.rationale.citation": '  - [{label}] "{quote}" ({source})',
    "draft.rationale.source": "{name} <{url}>",
    "draft.rationale.translation": "    Translation: {text}",
    "draft.not_in_sources.header": "**What the sources do not say**",
    "draft.review_points.header": "**To check first**",
    "draft.objection": "> **OBJECTION** ({type}, {direction}): {explanation}",
    "objection.type.misread_evidence": "evidence misread",
    "objection.type.insufficient_evidence": "insufficient evidence",
    "objection.type.not_independent": "sources not independent",
    "objection.type.wrong_period_or_scope": "wrong period or scope",
    "objection.type.different_definition": "different definition",
    "objection.type.unfaithful_framing": "unfaithful framing",
    "objection.direction.more_severe": "more severe",
    "objection.direction.less_severe": "less severe",
    "objection.direction.abstain": "abstain",
    "draft.warnings.header": "**Warnings**",
    "draft.warning.downgraded": "- Verdict downgraded by the independence rule.",
    "draft.warning.post_deleted": "- The post was deleted since its ingestion.",
    "draft.warning.post_modified": "- The post was modified since its ingestion.",
    "draft.warning.budget_incomplete": "- Budget exhausted: the collection is incomplete.",
    "draft.warning.cap_reached": "- Per-investigation cost cap reached: no escalation.",
    "draft.warning.refused": "- A model refused to answer ({roles}).",
    "draft.warning.invalid_citations": (
        "- {count} citation(s) not found verbatim in their passage (dropped)."
    ),
    "draft.warning.loaded_terms": "- Loaded terms in the note: {terms}.",
    "draft.warning.note_too_long": "- The note exceeds 280 characters.",
    "draft.warning.unknown_links": "- {count} link(s) of the note are not retrieved sources.",
    "draft.warning.sources_after_post": "- {count} source(s) are dated after the post.",
    "draft.warning.sources_too_old": "- {count} source(s) are too old for this claim.",
    "draft.warning.conflicting_sources": "- Sources contradict each other.",
    "draft.warning.critic_opinion": "- Critic: the note contains an opinion.",
    "draft.warning.critic_generalization": "- Critic: the note contains a generalization.",
    "draft.warning.critic_judges_author": "- Critic: the note judges the author.",
    "draft.warning.critic_loaded_words": "- Critic: loaded words ({words}).",
    "draft.warning.critic_unsourced": "- Critic: {count} unsourced fact(s).",
    "draft.warning.critic_comment": "- Critic: {text}",
    "draft.button.validate": "Validate",
    "draft.button.correct": "Correct",
    "draft.button.reject": "Reject",
    "draft.modal.correct.title": "Correct the draft",
    "draft.modal.veracity.label": "Veracity (optional)",
    "draft.modal.veracity.placeholder": "{values}",
    "draft.modal.note.label": "Note (optional, 280 chars + links)",
    "draft.modal.reason.label": "Reason (optional)",
    "draft.modal.reject.title": "Reject the draft",
    "draft.modal.reject.reason": "Reason (required)",
    "draft.bad_veracity": "Unknown veracity `{value}`. Expected: {values}.",
    "draft.review.endorsed": (
        "Your approval of draft `{short_id}` is recorded. An editor must now validate it."
    ),
    "draft.review.validated": "Draft `{short_id}` validated: the verdict is now {veracity}.",
    "draft.review.rejected": "Draft `{short_id}` rejected.",
    "factcheck.accepted": (
        "Fact-check requested for the {platform} post `{external_id}`. "
        "The draft will appear in the dossiers channel."
    ),
    "factcheck.duplicate": (
        "A fact-check of the {platform} post `{external_id}` is already queued."
    ),
    # --- processing profiles and level overrides -----------------------------------
    "error.TWR-FCK-029": "Profile version not found. ({code})",
    "error.TWR-FCK-030": "Profile refused: {message} ({code})",
    "error.TWR-FCK-031": "Invalid level override: {message} ({code})",
    "error.TWR-FCK-032": "Override target not found. ({code})",
    "profile.show.header": "Active profile: version {version} ({origin}).",
    "profile.show.default": "default from the settings",
    "profile.show.stored": "stored",
    "profile.show.general": (
        "Same-claim threshold {same} | grey zone {grey} | draft quota {quota}/day | "
        "E1 sample rate {sample}"
    ),
    "profile.show.level": (
        "- {level}: E1 threshold {e1} | direct reuse {reuse} | unit cap {cap} $ | "
        "monthly budget {budget} $"
    ),
    "profile.yes": "yes",
    "profile.no": "no",
    "profile.history.header": "Profile versions ({count}):",
    "profile.history.line": "- v{version}{active} | {author} | {created} | {reason}",
    "profile.history.active": " (active)",
    "profile.set.done": "Profile version {version} created and activated: {path} = {value}.",
    "profile.rollback.done": "Rolled back: profile version {version} is active again.",
    "level.set.done": (
        "Level {level} forced on the {target_type} `{target}` until {expires} "
        "(override `{short_id}`). {applied}"
    ),
    "level.set.applied": "Applied now.",
    "level.set.not_applied": "Recorded: not applied to a group yet or deferred.",
    "level.list.header": "Active level overrides ({count}):",
    "level.list.empty": "No active level override.",
    "level.list.line": (
        "- `{short_id}` | {target_type} `{target}` -> {level} | until {expires} | {reason}"
    ),
    "level.revoke.done": "Override `{short_id}` revoked.",
    # --- groups, reference facts and value bands -----------------------------------
    "error.TWR-FCK-020": "Claim group not found. ({code})",
    "error.TWR-FCK-021": "This group was already merged into another group. ({code})",
    "error.TWR-FCK-022": (
        "Both groups hold verdicts with different outcomes: a merge never overrides a verdict. "
        "({code})"
    ),
    "error.TWR-FCK-023": (
        "Only the group that absorbs the other may hold the active verdict. ({code})"
    ),
    "error.TWR-FCK-024": (
        "Invalid split: the claims must be active members and the group cannot be emptied. ({code})"
    ),
    "error.TWR-FCK-025": "Reference fact not found. ({code})",
    "error.TWR-FCK-026": "Invalid reference fact: {message} ({code})",
    "error.TWR-FCK-027": "This reference fact is not validated yet. ({code})",
    "error.TWR-FCK-028": "The value band cannot be validated: {message} ({code})",
    "draft.group.line": "**Group** `{group_id}` | {members} claim(s) | platforms: {platforms}",
    "draft.group.other_posts": "Other posts: {links}",
    "draft.group.related": (
        "Possible duplicate: group `{group_id}` already has a verdict ({veracity}). "
        'Use "Same group as..." to merge it.'
    ),
    "draft.fact.line": (
        "**Reference** {label}: {value} {unit} ({start} to {end}, {territory}, {status})"
    ),
    "draft.fact.band": "Band: {label}",
    "draft.fact.outdated": "The claimed value matches the previous figure: outdated.",
    "draft.external.header": "**Fact-checks elsewhere**",
    "draft.external.line": "- {publisher}: {rating} <{url}>",
    "fact.status.pending": "pending",
    "fact.status.validated": "validated",
    "fact.status.rejected": "rejected",
    "band.kind.contains": "contains the fact",
    "band.kind.within_1_tau": "within 1 tau of the fact",
    "band.kind.within_3_tau": "within 3 tau of the fact",
    "band.kind.beyond": "beyond 3 tau of the fact",
    "draft.button.samegroup": "Same group as...",
    "draft.button.split": "Split",
    "draft.button.fact": "Create fact",
    "draft.button.band": "Validate band",
    "draft.modal.samegroup.title": "Merge another group into this one",
    "draft.modal.samegroup.group": "Group id to absorb",
    "draft.modal.split.title": "Split claims off this group",
    "draft.modal.split.claims": "Claim ids (comma separated)",
    "draft.modal.fact.title": "Create a reference fact",
    "draft.modal.fact.value": "Value",
    "draft.modal.fact.period": "Period (YYYY-MM-DD YYYY-MM-DD)",
    "draft.modal.fact.url": "Source URL",
    "draft.modal.fact.name": "Source name",
    "draft.modal.fact.published": "Published on (YYYY-MM-DD)",
    "group.merged": "Merge done: group `{group_id}` now has {members} claim(s).",
    "group.split": "Split done: the claims moved to the new group `{group_id}` (no verdict yet).",
    "group.show.header": "Group `{group_id}` | {level} | {state} | {members} claim(s)",
    "group.show.merged_into": "Merged into `{group_id}`.",
    "group.show.verdict": "Verdict: {veracity} (expires {expires}, rule {rule})",
    "group.show.fact": "Fact: {label} = {value} {unit} | band {band} ({side})",
    "group.show.member": "- `{claim_id}` {text} {links}",
    "group.show.event": "  journal: {kind} by {actor} on {when}",
    "fact.line": (
        "- `{short_id}` {status} | {label}: {value} {unit} | {start} to {end} | {territory}"
    ),
    "fact.show.source": "Source: {source} <{url}> | published {published} | next {next}",
    "fact.list.header": "Reference facts ({count}):",
    "fact.list.empty": "No reference fact.",
    "fact.validated": "Fact `{short_id}` validated.",
    "fact.rejected": "Fact `{short_id}` rejected.",
    "fact.created": "Fact `{short_id}` created ({label}). A reviewer must validate it.",
    "fact.needs_validated": "Validate the verdict first: a fact is created from an active verdict.",
    "band.validated": "Band validated: {label} ({side}) -> {veracity}.",
}
