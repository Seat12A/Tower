"""``/factcheck``, ``/group`` and ``/fact``: cards with persistent buttons and Modals V2.

Persistent components (custom ids ``t12a:grp:...`` and ``t12a:fact:...``): the buttons of
the group and reference-fact cards and the select of ``/fact list``. Reasons are asked in
modals opened from the buttons; the slash commands keep plain options for scripting.
The draft buttons live in ``commands/drafts.py``.
"""

import functools
import re
from typing import Any, Self, cast

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import factcheck as fc
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

_UUID = r"[0-9a-fA-F-]{36}"
_GROUP_TEMPLATE = rf"t12a:grp:(?P<action>{'|'.join(fc.GROUP_ACTIONS)}):(?P<group>{_UUID})"
_FACT_TEMPLATE = rf"t12a:fact:(?P<action>{'|'.join(fc.FACT_ACTIONS)}):(?P<fact>{_UUID})"
_FACT_PICK_TEMPLATE = re.escape(fc.fact_custom_id("open"))

_SAMPLE_UUID = "00000000-0000-0000-0000-000000000000"
SAMPLE_CUSTOM_IDS = (
    fc.group_custom_id("show", _SAMPLE_UUID),
    fc.fact_custom_id("show", _SAMPLE_UUID),
    fc.fact_custom_id("open"),
)


def text_input(
    locale: str,
    key: str,
    *,
    paragraph: bool = False,
    required: bool = True,
    max_length: int = 500,
    min_length: int | None = None,
    default: str | None = None,
) -> tuple[ui.TextInput[Any], ui.Label[Any]]:
    """A text input wrapped in its ``ui.Label`` (``<key>`` = label, ``<key>.hint`` =
    description when the catalogue has one). Returns ``(input, label)``."""
    field: ui.TextInput[Any] = ui.TextInput(
        style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
        required=required,
        max_length=max_length,
        min_length=min_length,
        default=default,
    )
    hint = t(locale, f"{key}.hint")
    return field, ui.Label(
        text=t(locale, key)[:45],
        description=None if hint == f"{key}.hint" else hint[:100],
        component=field,
    )


@functools.cache
def _kit(ctx: Context) -> Any:
    gate = ctx.gate

    class GroupMergeModal(BaseModal):
        def __init__(self, group_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "group.modal.merge.title")[:45])
            self.group_id = group_id
            self.source, label = text_input(
                locale, "group.modal.source", min_length=6, max_length=36
            )
            self.add_item(label)
            self.reason, label = text_input(locale, "fc.modal.reason", paragraph=True)
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            source, reason = self.source.value, self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: fc.group_merge(gate, uid, loc, self.group_id, source, reason),
                edit=True,
            )

    class GroupSplitModal(BaseModal):
        def __init__(self, group_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "group.modal.split.title")[:45])
            self.group_id = group_id
            self.claims, label = text_input(
                locale, "group.modal.claims", paragraph=True, max_length=1500
            )
            self.add_item(label)
            self.reason, label = text_input(locale, "fc.modal.reason", paragraph=True)
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            claims, reason = self.claims.value, self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: fc.group_split(gate, uid, loc, self.group_id, claims, reason),
                edit=True,
            )

    class FactRejectModal(BaseModal):
        def __init__(self, fact_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "fact.modal.reject.title")[:45])
            self.fact_id = fact_id
            self.reason, label = text_input(locale, "fc.modal.reason", paragraph=True)
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            reason = self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: fc.fact_reject(gate, uid, loc, self.fact_id, reason),
                edit=True,
            )

    class GroupButton(ui.DynamicItem[ui.Button[Any]], template=_GROUP_TEMPLATE):
        def __init__(self, action: str, group_id: str) -> None:
            super().__init__(ui.Button(custom_id=fc.group_custom_id(action, group_id)))
            self.action = action
            self.group_id = group_id

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["action"], match["group"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            locale = interaction_locale(interaction)
            if self.action == "merge":
                await interaction.response.send_modal(GroupMergeModal(self.group_id, locale))
            elif self.action == "split":
                await interaction.response.send_modal(GroupSplitModal(self.group_id, locale))
            else:
                await run_handler(
                    interaction,
                    lambda loc, uid: fc.group_show(gate, uid, loc, self.group_id),
                    edit=True,
                )

    class FactButton(ui.DynamicItem[ui.Button[Any]], template=_FACT_TEMPLATE):
        def __init__(self, action: str, fact_id: str) -> None:
            super().__init__(ui.Button(custom_id=fc.fact_custom_id(action, fact_id)))
            self.action = action
            self.fact_id = fact_id

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["action"], match["fact"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            if self.action == "reject":
                locale = interaction_locale(interaction)
                await interaction.response.send_modal(FactRejectModal(self.fact_id, locale))
            elif self.action == "validate":
                await run_handler(
                    interaction,
                    lambda loc, uid: fc.fact_validate(gate, uid, loc, self.fact_id),
                    edit=True,
                )
            else:
                await run_handler(
                    interaction,
                    lambda loc, uid: fc.fact_show(gate, uid, loc, self.fact_id),
                    edit=True,
                )

    class FactPick(ui.DynamicItem[ui.Select[Any]], template=_FACT_PICK_TEMPLATE):
        def __init__(self) -> None:
            super().__init__(ui.Select(custom_id=fc.fact_custom_id("open")))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            values = list(cast(dict[str, Any], interaction.data or {}).get("values", []))
            if values:
                fact_id = str(values[0])
                await run_handler(
                    interaction, lambda loc, uid: fc.fact_show(gate, uid, loc, fact_id)
                )

    return GroupButton, FactButton, FactPick


def dynamic_items(ctx: Context) -> list[type]:
    return list(_kit(ctx))


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate

    @app_commands.command(name="factcheck", description="Request a fact-check of a post")
    @app_commands.guild_only()
    @app_commands.describe(url="https://x.com/<account>/status/<id>")
    async def fc_request(interaction: discord.Interaction, url: str) -> None:
        await run_handler(interaction, lambda loc, uid: fc.request(gate, uid, loc, url))

    class NoteModal(BaseModal):
        """Free texts of ``/note``; the choices come from the slash command options."""

        def __init__(self, locale: str, options: dict[str, str | None]) -> None:
            super().__init__(title=t(locale, "note.modal.title")[:45])
            self.options = options
            self.claim, label = text_input(
                locale, "note.modal.claim", paragraph=True, max_length=300
            )
            self.add_item(label)
            self.note, label = text_input(
                locale, "note.modal.note", paragraph=True, required=False, max_length=1000
            )
            self.add_item(label)
            self.links, label = text_input(
                locale, "note.modal.links", paragraph=True, required=False, max_length=2000
            )
            self.add_item(label)
            self.sheet, label = text_input(
                locale, "note.modal.sheet", paragraph=True, required=False, max_length=4000
            )
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            o = self.options
            claim, note = self.claim.value, self.note.value
            links, sheet = self.links.value, self.sheet.value
            await run_handler(
                interaction,
                lambda loc, uid: fc.manual_note(
                    gate,
                    uid,
                    loc,
                    url=o["url"] or "",
                    claim_type=o["claim_type"] or "",
                    veracity=o["veracity"] or "",
                    claim_text=claim,
                    context_need=o["context_need"],
                    context_types=o["context_types"],
                    note_text=note,
                    note_lang=o["note_lang"] or "fr",
                    links=links,
                    fact_sheet=sheet,
                ),
            )

    def _choices(values: tuple[str, ...]) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=v, value=v) for v in values]

    @app_commands.command(name="note", description="Write a fact-check note by hand (no AI)")
    @app_commands.guild_only()
    @app_commands.describe(
        url="https://x.com/<account>/status/<id>",
        veracity="Outcome of the claim",
        claim_type="Kind of claim",
        context_need="Default: from the outcome",
        context_types="Context types, comma separated (e.g. cherry_picking,outdated)",
        note_lang="Language of the note",
    )
    @app_commands.choices(
        veracity=_choices(fc.MANUAL_VERACITIES),
        claim_type=_choices(fc.MANUAL_CLAIM_TYPES),
        context_need=_choices(fc.MANUAL_CONTEXT_NEEDS),
        note_lang=_choices(("fr", "en")),
    )
    async def note_cmd(
        interaction: discord.Interaction,
        url: str,
        veracity: app_commands.Choice[str],
        claim_type: app_commands.Choice[str],
        context_need: app_commands.Choice[str] | None = None,
        context_types: str | None = None,
        note_lang: app_commands.Choice[str] | None = None,
    ) -> None:
        options: dict[str, str | None] = {
            "url": url,
            "veracity": veracity.value,
            "claim_type": claim_type.value,
            "context_need": context_need.value if context_need else None,
            "context_types": context_types,
            "note_lang": note_lang.value if note_lang else "fr",
        }
        await interaction.response.send_modal(NoteModal(interaction_locale(interaction), options))

    grp = app_commands.Group(name="group", description="Claim groups", guild_only=True)

    @grp.command(name="show", description="Show a claim group")
    @app_commands.describe(group_id="Group id")
    async def g_show(interaction: discord.Interaction, group_id: str) -> None:
        await run_handler(interaction, lambda loc, uid: fc.group_show(gate, uid, loc, group_id))

    @grp.command(name="merge", description="Merge the source group into the target group")
    @app_commands.describe(target="Group that absorbs", source="Group absorbed", reason="Why")
    async def g_merge(
        interaction: discord.Interaction, target: str, source: str, reason: str
    ) -> None:
        await run_handler(
            interaction, lambda loc, uid: fc.group_merge(gate, uid, loc, target, source, reason)
        )

    @grp.command(name="split", description="Move claims into a new group")
    @app_commands.describe(group_id="Group id", claim_ids="Claim ids, comma separated")
    async def g_split(
        interaction: discord.Interaction, group_id: str, claim_ids: str, reason: str
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: fc.group_split(gate, uid, loc, group_id, claim_ids, reason),
        )

    fct = app_commands.Group(
        name="fact", description="Reference facts and value bands", guild_only=True
    )
    status_choices = [
        app_commands.Choice(name=v, value=v) for v in ("pending", "validated", "rejected")
    ]

    @fct.command(name="show", description="Show a reference fact")
    @app_commands.describe(fact_id="Fact id (at least 6 characters)")
    async def f_show(interaction: discord.Interaction, fact_id: str) -> None:
        await run_handler(interaction, lambda loc, uid: fc.fact_show(gate, uid, loc, fact_id))

    @fct.command(name="list", description="List reference facts")
    @app_commands.choices(status=status_choices)
    async def f_list(
        interaction: discord.Interaction, status: app_commands.Choice[str] | None = None
    ) -> None:
        value = status.value if status else None
        await run_handler(interaction, lambda loc, uid: fc.fact_list(gate, uid, loc, value))

    @fct.command(name="validate", description="Validate a pending reference fact")
    async def f_validate(interaction: discord.Interaction, fact_id: str) -> None:
        await run_handler(interaction, lambda loc, uid: fc.fact_validate(gate, uid, loc, fact_id))

    @fct.command(name="reject", description="Reject a pending reference fact")
    async def f_reject(interaction: discord.Interaction, fact_id: str, reason: str) -> None:
        await run_handler(
            interaction, lambda loc, uid: fc.fact_reject(gate, uid, loc, fact_id, reason)
        )

    @fct.command(name="band", description="Validate the value band of an active verdict")
    @app_commands.describe(verdict_id="Verdict (draft) id")
    async def f_band(interaction: discord.Interaction, verdict_id: str) -> None:
        await run_handler(
            interaction, lambda loc, uid: fc.band_validate(gate, uid, loc, verdict_id)
        )

    commands: tuple[Any, ...] = (fc_request, note_cmd, grp, fct)
    for command in commands:
        tree.add_command(command)
