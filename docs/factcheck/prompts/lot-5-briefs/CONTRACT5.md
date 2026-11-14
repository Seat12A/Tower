# Lot 5 data contract (shared by H2-H6)

Read `COMMON.md` (rules unchanged; lot 5 codes are `TWR-FCK-039..045`, already defined).
Lot 4 is implemented (see `docs/fact-check.md`, section "Lot 4"): E1 routing
(`content_embeddings`, level N0/N1, `sampled`), E3 matching in `record_extraction`,
groups, reuse, reference facts, profiles (`active_profile`), level overrides.

Already committed for lot 5 (read, do not change unless your brief says so):
- Migration `0007`, models `core/models/factcheck_v2.py` (`ExtractionBatch`,
  `ContentExtraction`, `ExtractionAudit`, `SchedulerRun`, `Narrative`, `NarrativeGroup`,
  `NarrativeEvent`, `NarrativeStat` (column `span`: `5m`/`1h`/`6h`), `NarrativeNovelty`,
  `EntityRarity`) and new `claim_groups` columns (`light_signal`, `light_checked_at`,
  `light_evidence`, `observers_6h`, `velocity`, `priority`, `level_changed_at`,
  `below_threshold_since`).
- Values (`core/domain/factcheck/values.py`, bottom): `ExtractionStatus`, `BatchStatus`,
  `LightSignal`, `NarrativeState` (`active`, `watch`, `surge`, `alert`, `closed`),
  `TitleStatus`, `NarrativeEventKind`, `NoveltyKind`.
- Names (`core/factcheck/names.py`): jobs `JOB_EXTRACT_BATCH`, `JOB_LIGHT_CHECK`,
  `JOB_ATTACH_NARRATIVE`, `JOB_NARRATIVE_TITLE`, `JOB_PROMOTION_CHECK`; topics
  `NARRATIVE_SURGE_DETECTED`, `NARRATIVE_SURGE_ENDED`, `NARRATIVE_MERGED`,
  `NARRATIVE_SPLIT`; roles `ROLE_EXTRACT_BATCH`, `ROLE_LIGHT_CHECK`,
  `ROLE_NARRATIVE_TITLE`; keys `extract_batch_key`, `light_check_key`, `attach_key`,
  `title_key`, `promotion_key`.
- Schemas `ExtractBatchOutput`, `LightCheckOutput`, `NarrativeTitleOutput`; inputs
  `extract_batch_input`, `light_check_input`, `narrative_title_input`; prompts
  `services/factcheck/prompts/{extract_batch,light_check,narrative_title}.md`; fake
  outputs for the three roles in `core/adapters/fake_llm.py` (do not edit them unless your
  brief says so); route `extract_batch` seeded.
- Settings (`core/config.py`, section "Fact-check V2"): `fc_mass_flow`, `fc_batch_*`,
  `fc_canary_rate`, `fc_solo_audit_rate`, `fc_scheduler_*`, `fc_promotion_*`,
  `fc_demotion_hours`, `fc_light_check_share`, `fc_deep_queue_capacity`,
  `fc_narrative_*`, `fc_wave_thresholds` (JSON), `fc_watched_themes`,
  `fc_surge_daily_quota`, `fc_surge_end_hours`, `fc_surge_verdict_ttl_hours`,
  `fc_sampling_per_minute`, `fc_novelty_*`.
- Service registration (`services/factcheck/__init__.py`) is DONE and points to STUB
  functions you replace (keep the exact names and signatures):
  - `services/factcheck/batching.py`: `compose_batches(session, settings) -> int`
    (periodic `factcheck:compose_batches`, 60 s), `extract_batch(ctx)` (job);
  - `services/factcheck/scheduler.py`: `schedule(session, settings) -> int` (periodic
    singleton `factcheck:schedule`, ticks every 60 s), `promotion_check(ctx)` (job);
  - `services/factcheck/light_check.py`: `light_check(ctx)` (job);
  - `services/factcheck/narratives.py`: `on_group_created(session, events)`
    (subscription on `claim_group.created`), `attach_narrative(ctx)`,
    `narrative_title(ctx)` (jobs), `waves_step(session, settings, now) -> dict`
    (called by the scheduler pass), `maintain_narratives(session, settings) -> int`
    (periodic `factcheck:narratives`, 1 h), `filter_for_surges(session, settings,
    content_ids) -> (to_extract, {content_id: narrative_id})` (called by the batch
    composer).
  Periodic task functions receive a session in a transaction; a periodic task may enqueue
  jobs in it. Heavy or network work goes into jobs.

## Binding decisions

1. **N1 path** (`fc_mass_flow=True`): a content is pending for batch extraction when its
   `content_embeddings.level = 'N1'` or `sampled = true`, its language is processed, it is
   not a copy, its author is not an active watched account (N3 stays immediate, lot 3),
   and it has no `content_extractions` row. Extraction at level `N1`.
2. `record_extraction` gains `investigate: bool = True`. N1 (and N2 created by the batch
   path) groups are created with state `dormant` and **no investigation**: the scheduler
   decides. Joining and reuse rules of lot 4 are unchanged.
3. Every extraction (solo or batch, any level) writes/updates one `content_extractions`
   row (`extracted` or `no_claim`; `sampled_out` for surge sampling).
4. Scheduler pass (singleton): adaptive interval `clamp(min, max, 3600 × target ÷ rate)`
   with `rate` = new contents in the last hour; steps: group metrics → promotions and
   demotions → `waves_step` → priority → enqueue (investigations for N2 within budget
   `level:N2`, daily draft quota and fc-deep capacity; light checks for the top
   `fc_light_check_share` of new N1 groups). One `scheduler_runs` row per pass. N3/N4
   never wait for the scheduler.
5. Promotion N1 → N2 (one condition suffices): observers (distinct installation
   pseudonyms over the group's contents) ≥ `fc_promotion_observers` in 6 h; velocity
   (observations last hour ÷ previous hour, with at least 10 observations) ≥
   `fc_promotion_velocity_factor`; views (max `impression_count`/`views` of its contents)
   > `fc_promotion_views`; light signal `likely_false` with reach above the median of
   active N1 groups; group in a narrative at `surge` or above. Confirmation before any
   spending: job `factcheck.promotion_check` reads the representative post's metrics
   through `PlatformClient.get_metrics` (budget `provider:x`); no platform client for the
   platform ⇒ promote with `flags.unconfirmed`. Demotion N2 → N1 after
   `fc_demotion_hours` below every condition, never during a running investigation.
   Every change: `claim_groups.level`, `level_changed_at`, a `level_changed` group event,
   `claim_group.level_changed` (reason `promotion`/`demotion`/`wave`).
6. Priority (pure, no author term): `reach × theme_risk × verifiability × novelty ×
   wave_factor ÷ cost(level)` with `reach = log1p(views) + 2 log1p(observers) +
   velocity`; theme risk 3 for health/elections/security/disaster, 2 for
   economy/justice/immigration/international, else 1; novelty 1 (new) or 0.3
   (revalidation); wave factor 1, 2 (watch), 3 (surge), 5 (alert); cost = mean cost of the
   level's investigations over 30 days (floor 1 cent).
7. E5 light check: never published. Numeric claim with a validated fact and a band ⇒
   deterministic signal, no model (`contains` → `likely_true`, `within_1_tau` →
   `needs_context`, other bands → `likely_false`). Otherwise evidence = validated facts,
   existing fact-checks and structured sources of the Atlas (no encyclopedia, **no web
   search**), or, for a group of a surging narrative, the kept passages of the
   narrative's representative investigation (evidence dossier); role `light_check`.
   Result in `claim_groups.light_signal/light_checked_at/light_evidence`.
8. Narratives: every new group is attached (job) to an existing active narrative when
   ALL of: ≥ 2 signals of 4 with ≥ 1 non-semantic (semantic: cosine to a medoid ≥
   `fc_narrative_semantic_threshold`; co-occurrence: ≥ 3 posts from > 3 accounts carry a
   claim of both; shared entity; shared media), ≥ 1 specific anchor (rare entity: ≤
   `fc_narrative_rare_entity_max` claims over 30 days; non-generic media: used in ≤ 3
   narratives; shared link), and cosine to at least one medoid ≥
   `fc_narrative_medoid_floor`. Otherwise the group founds a new narrative. Medoids:
   founder (fixed) + 2 highest-reach groups, re-elected at most daily. Closure after
   `fc_narrative_close_days` of inactivity; a later match on a closed narrative creates a
   new one with `previous_narrative_id`. Split suggested above `fc_narrative_split_groups`.
9. Waves: tiers from the 1 h window (thresholds JSON): watch (share ≥ 1 %, accounts ≥ 10),
   surge (share ≥ 3 %, accounts ≥ 30 and growth ≥ 2× the previous hour; or watch on 2
   platforms separately; or a group of the narrative at N3), alert (share ≥ 8 % and
   accounts ≥ 80; or a watched theme at watch or above). Growth is ignored for 24 h after
   an extension filter version change. Up immediately; surge/alert end after
   `fc_surge_end_hours` below watch (`narrative.surge_ended` with a report). Entering surge:
   groups ≥ N2 (alert: N4), representative group investigated once (jobs priority high),
   other groups light-checked on the dossier; `narrative.surge_detected` (payload:
   `narrative_id`, `tier`, `share`, `accounts`, `platforms`, `title`). Surge drafts use a
   separate daily quota `fc_surge_daily_quota`; verdicts validated in a surge expire
   within `fc_surge_verdict_ttl_hours`.
10. Sampling and novelty: beyond `fc_sampling_per_minute` posts per minute close (cosine ≥
    0.85 to a medoid) to a surging narrative, only a stratified random sample (platform,
    hour) is extracted; the rest is `sampled_out` (counted). Novelty fingerprints (code
    only: normalized numbers, links, mentions, media hashes, capitalized entities) of
    sampled-out posts are counted in `narrative_novelty`; when one reaches
    `fc_novelty_posts` posts from `fc_novelty_accounts` accounts (or a post above the
    narrative's 90th percentile of engagement), its most engaged post is forced into
    extraction, at most `fc_novelty_budget_share` of the narrative's extractions per hour.
11. Coordination signals are informative only (draft display), never in verdicts or
    priority, never based on a supposed political side.

## Topics and payloads (ids as strings, public UUIDs where they exist)

| Topic | Payload |
|---|---|
| `narrative.surge_detected` | `narrative_id`, `tier` (`surge`/`alert`), `share`, `accounts`, `platforms`, `title`, `representative_group_id` |
| `narrative.surge_ended` | `narrative_id`, `duration_s`, `posts`, `platforms`, `cost_usd_micros`, `first_draft_delay_s` |
| `narrative.merged` | `target_id`, `source_id`, `group_ids`, `actor`, `reason` |
| `narrative.split` | `narrative_id`, `new_narrative_id`, `group_ids`, `actor`, `reason` |
| `claim_group.level_changed` | as lot 4, reasons add `promotion`, `demotion`, `wave` |
