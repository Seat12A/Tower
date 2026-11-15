"""Persistent buttons and modals of the fact-check draft cards.

Draft messages carry ``t12a:fc:<action>:<draft id>`` buttons; a second dynamic item keeps
the legacy ``fc:<action>:<draft id>`` ids of messages posted before the redesign working.
Every click re-fetches the draft from gate. After a final decision the decision buttons
are removed from the message (``settle_view``); once the verdict is validated the group
and reference-fact buttons stay: creating a fact or validating a band needs a validated
verdict.
"""

import functools
import re
from typing import Any, Self

import discord
from discord import ui

from tower.core.logging import get_logger
from tower.intercom.commands import Context
from tower.intercom.commands.factcheck import text_input
from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply, error_reply
from tower.intercom.handlers import factcheck as fc
from tower.intercom.i18n import t
from tower.intercom.ui import render
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

log = get_logger(__name__)

_ACTIONS = "|".join(fc.ACTIONS)
_UUID = "[0-9a-fA-F-]{36}"
_TEMPLATE = rf"t12a:fc:(?P<action>{_ACTIONS}):(?P<draft>{_UUID})"
_LEGACY_TEMPLATE = rf"fc:(?P<action>{_ACTIONS}):(?P<draft>{_UUID})"

_SAMPLE_UUID = "00000000-0000-0000-0000-000000000000"
SAMPLE_CUSTOM_IDS = (fc.custom_id("validate", _SAMPLE_UUID), f"fc:validate:{_SAMPLE_UUID}")

_NOTE_PREFILL_MAX = 1000


def settle_view(view: ui.LayoutView, *, status: object, locale: str) -> bool:
    """Remove the decision buttons of a draft card (``view``) once the draft is settled.

    ``status`` is the draft status read from gate: ``active`` (validated) keeps the group
    and reference-fact buttons, anything else removes every button. A grey line and the
    accent record the decision. Returns whether something changed."""
    keep_group = status == "active"
    changed = False
    for child in list(view.children):
        if isinstance(child, ui.ActionRow):
            view.remove_item(child)
            changed = True
    for container in view.children:
        if not isinstance(container, ui.Container):
            continue
        for row in list(container.children):
            if not isinstance(row, ui.ActionRow) or keep_group:
                continue
            children = list(container.children)
            index = children.index(row)
            container.remove_item(row)
            # The field label above the row, and the separator above the label.
            if index > 0 and isinstance(children[index - 1], ui.TextDisplay):
                container.remove_item(children[index - 1])
                if index > 1 and isinstance(children[index - 2], ui.Separator):
                    container.remove_item(children[index - 2])
            changed = True
        key = f"draft.settled.{status}"
        line = t(locale, key)
        if line != key:
            container.add_item(ui.TextDisplay(f"-# {line}"))
        colour = render.COLORS["success" if status == "active" else "neutral"]
        container.accent_colour = discord.Colour(colour)
    return changed


@functools.cache
def _kit(ctx: Context) -> Any:
    gate: GateClient = ctx.gate
    default_locale = ctx.default_locale

    async def settle(interaction: discord.Interaction, reply: Reply | None, draft_id: str) -> None:
        """After a final decision: strip the decision buttons from the clicked message."""
        message = interaction.message
        if reply is None or not reply.final or message is None:
            return
        try:
            status = (await gate.get_draft(interaction.user.id, draft_id)).get("status")
            view = ui.LayoutView.from_message(message, timeout=None)
            if settle_view(view, status=status, locale=default_locale):
                view.stop()
                await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except (GateError, discord.HTTPException) as exc:
            log.warning("intercom.draft_settle_failed", error=type(exc).__name__)

    class CorrectModal(BaseModal):
        def __init__(self, draft: dict[str, Any], locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.correct.title")[:45])
            self.draft_id = str(draft["draft_id"])
            self.current = str(draft.get("veracity"))
            self.radio: ui.RadioGroup[Any] = ui.RadioGroup(
                options=[
                    discord.RadioGroupOption(
                        label=fc.veracity_label(locale, v), value=v, default=v == self.current
                    )
                    for v in fc.VERACITIES
                ],
            )
            self.add_item(
                ui.Label(text=t(locale, "draft.modal.veracity.label")[:45], component=self.radio)
            )
            notes: dict[str, dict[str, Any]] = draft.get("notes") or {}
            existing = str((notes.get(locale) or {}).get("text") or "")
            self.note_default = existing if 0 < len(existing) <= _NOTE_PREFILL_MAX else ""
            self.note, label = text_input(
                locale,
                "draft.modal.note.label",
                paragraph=True,
                required=False,
                max_length=_NOTE_PREFILL_MAX,
                default=self.note_default or None,
            )
            self.add_item(label)
            self.reason, label = text_input(
                locale, "draft.modal.reason.label", paragraph=True, required=False
            )
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            chosen = self.radio.value
            veracity = None if chosen in (None, self.current) else chosen
            note = None if self.note.value.strip() == self.note_default else self.note.value
            reason = self.reason.value
            reply = await run_handler(
                interaction,
                lambda loc, uid: fc.review_edit(
                    gate, uid, loc, self.draft_id, veracity, note, reason
                ),
            )
            await settle(interaction, reply, self.draft_id)

    class RejectModal(BaseModal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.reject.title")[:45])
            self.draft_id = draft_id
            self.reason, label = text_input(locale, "draft.modal.reject.reason", paragraph=True)
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            reason = self.reason.value
            reply = await run_handler(
                interaction,
                lambda loc, uid: fc.review_reject(gate, uid, loc, self.draft_id, reason),
            )
            await settle(interaction, reply, self.draft_id)

    class SameGroupModal(BaseModal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.samegroup.title")[:45])
            self.draft_id = draft_id
            self.group, label = text_input(
                locale, "draft.modal.samegroup.group", min_length=36, max_length=36
            )
            self.add_item(label)
            self.reason, label = text_input(
                locale, "draft.modal.reason.label", paragraph=True, required=False
            )
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            group, reason = self.group.value, self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: fc.group_merge_same(gate, uid, loc, self.draft_id, group, reason),
            )

    class SplitModal(BaseModal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.split.title")[:45])
            self.draft_id = draft_id
            self.claims, label = text_input(
                locale, "draft.modal.split.claims", paragraph=True, max_length=1500
            )
            self.add_item(label)
            self.reason, label = text_input(
                locale, "draft.modal.reason.label", paragraph=True, required=False
            )
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            claims, reason = self.claims.value, self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: fc.group_split_draft(
                    gate, uid, loc, self.draft_id, claims, reason
                ),
            )

    class FactModal(BaseModal):
        _FIELDS = (("value", 40), ("period", 40), ("url", 400), ("name", 200), ("published", 12))

        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.fact.title")[:45])
            self.draft_id = draft_id
            self.inputs: dict[str, ui.TextInput[Any]] = {}
            for key, max_length in self._FIELDS:
                field, label = text_input(locale, f"draft.modal.fact.{key}", max_length=max_length)
                self.inputs[key] = field
                self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            v = {k: f.value for k, f in self.inputs.items()}
            await run_handler(
                interaction,
                lambda loc, uid: fc.fact_create(
                    gate,
                    uid,
                    loc,
                    self.draft_id,
                    v["value"],
                    v["period"],
                    v["url"],
                    v["name"],
                    v["published"],
                ),
            )

    class DraftButton(ui.DynamicItem[ui.Button[Any]], template=_TEMPLATE):
        prefix = fc.DRAFT_PREFIX

        def __init__(self, action: str, draft_id: str) -> None:
            super().__init__(ui.Button(custom_id=f"{self.prefix}{action}:{draft_id}"))
            self.action = action
            self.draft_id = draft_id

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["action"], match["draft"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            locale = interaction_locale(interaction)
            try:
                # Every click works from the draft as gate has it now.
                draft = await gate.get_draft(interaction.user.id, self.draft_id)
            except GateError as exc:
                await render.send(interaction, error_reply(locale, exc))
                return
            draft.setdefault("draft_id", self.draft_id)
            action = self.action
            if action in fc.DECISION_ACTIONS and draft.get("status") != "draft":
                await render.send(interaction, fc.decided_reply(locale, draft.get("status")))
                await settle(
                    interaction,
                    Reply(final=True),
                    self.draft_id,
                )
                return
            if action == "validate":
                reply = await run_handler(
                    interaction, lambda loc, uid: fc.review_accept(gate, uid, loc, self.draft_id)
                )
                await settle(interaction, reply, self.draft_id)
            elif action == "band":
                await run_handler(
                    interaction, lambda loc, uid: fc.band_validate(gate, uid, loc, self.draft_id)
                )
            elif action == "correct":
                # A modal must be the first response: no defer.
                await interaction.response.send_modal(CorrectModal(draft, locale))
            elif action == "reject":
                await interaction.response.send_modal(RejectModal(self.draft_id, locale))
            elif action == "samegroup":
                await interaction.response.send_modal(SameGroupModal(self.draft_id, locale))
            elif action == "split":
                await interaction.response.send_modal(SplitModal(self.draft_id, locale))
            else:
                await interaction.response.send_modal(FactModal(self.draft_id, locale))

    class LegacyDraftButton(DraftButton, template=_LEGACY_TEMPLATE):
        """Buttons of draft messages posted before the redesign (``fc:<action>:<id>``)."""

        prefix = fc.LEGACY_DRAFT_PREFIX

    return DraftButton, LegacyDraftButton


def dynamic_items(ctx: Context) -> list[type]:
    return list(_kit(ctx))


def register(tree: Any, ctx: Context) -> None:
    """No slash command: drafts are posted by the notifier."""
