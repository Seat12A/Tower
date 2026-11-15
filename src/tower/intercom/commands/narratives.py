"""``/narrative``: cards with persistent buttons (``t12a:nar:...``) and Modals V2.

``open`` (on public wave notifications) sends the narrative as a new private message;
``show`` refreshes a private panel in place. Merge, split and title open a modal.
"""

import functools
import re
from typing import Any, Self, cast

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.commands.factcheck import text_input
from tower.intercom.handlers import narratives as nar
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

_BUTTON_TEMPLATE = (
    rf"t12a:nar:(?P<action>{'|'.join(nar.NARRATIVE_ACTIONS)}):(?P<narrative>[0-9a-fA-F-]{{36}})"
)
_PICK_TEMPLATE = re.escape(nar.narrative_custom_id(nar.PICK_ACTION))

_SAMPLE_UUID = "00000000-0000-0000-0000-000000000000"
SAMPLE_CUSTOM_IDS = (
    nar.narrative_custom_id("open", _SAMPLE_UUID),
    nar.narrative_custom_id(nar.PICK_ACTION),
)


@functools.cache
def _kit(ctx: Context) -> Any:
    gate = ctx.gate

    class MergeModal(BaseModal):
        def __init__(self, narrative_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "narrative.modal.merge.title")[:45])
            self.narrative_id = narrative_id
            self.source, label = text_input(
                locale, "narrative.modal.source", min_length=36, max_length=36
            )
            self.add_item(label)
            self.reason, label = text_input(locale, "fc.modal.reason", paragraph=True)
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            source, reason = self.source.value, self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: nar.narrative_merge(
                    gate, uid, loc, self.narrative_id, source, reason
                ),
                edit=True,
            )

    class SplitModal(BaseModal):
        def __init__(self, narrative_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "narrative.modal.split.title")[:45])
            self.narrative_id = narrative_id
            self.groups, label = text_input(
                locale, "narrative.modal.groups", paragraph=True, max_length=1500
            )
            self.add_item(label)
            self.reason, label = text_input(locale, "fc.modal.reason", paragraph=True)
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            groups, reason = self.groups.value, self.reason.value
            await run_handler(
                interaction,
                lambda loc, uid: nar.narrative_split(
                    gate, uid, loc, self.narrative_id, groups, reason
                ),
                edit=True,
            )

    class TitleModal(BaseModal):
        def __init__(self, narrative_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "narrative.modal.title.title")[:45])
            self.narrative_id = narrative_id
            self.title_input, label = text_input(
                locale, "narrative.modal.title", required=False, max_length=200
            )
            self.add_item(label)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            title = self.title_input.value
            await run_handler(
                interaction,
                lambda loc, uid: nar.narrative_title(gate, uid, loc, self.narrative_id, title),
                edit=True,
            )

    class NarrativeButton(ui.DynamicItem[ui.Button[Any]], template=_BUTTON_TEMPLATE):
        def __init__(self, action: str, narrative_id: str) -> None:
            super().__init__(ui.Button(custom_id=nar.narrative_custom_id(action, narrative_id)))
            self.action = action
            self.narrative_id = narrative_id

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["action"], match["narrative"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            locale = interaction_locale(interaction)
            nid = self.narrative_id
            if self.action == "merge":
                await interaction.response.send_modal(MergeModal(nid, locale))
            elif self.action == "split":
                await interaction.response.send_modal(SplitModal(nid, locale))
            elif self.action == "title":
                await interaction.response.send_modal(TitleModal(nid, locale))
            elif self.action == "titleok":
                await run_handler(
                    interaction,
                    lambda loc, uid: nar.narrative_title(gate, uid, loc, nid, None),
                    edit=True,
                )
            else:
                await run_handler(
                    interaction,
                    lambda loc, uid: nar.narrative_show(gate, uid, loc, nid),
                    edit=self.action == "show",
                )

    class NarrativePick(ui.DynamicItem[ui.Select[Any]], template=_PICK_TEMPLATE):
        def __init__(self) -> None:
            super().__init__(ui.Select(custom_id=nar.narrative_custom_id(nar.PICK_ACTION)))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            values = list(cast(dict[str, Any], interaction.data or {}).get("values", []))
            if values:
                nid = str(values[0])
                await run_handler(
                    interaction, lambda loc, uid: nar.narrative_show(gate, uid, loc, nid)
                )

    return NarrativeButton, NarrativePick


def dynamic_items(ctx: Context) -> list[type]:
    return list(_kit(ctx))


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    group = app_commands.Group(
        name="narrative", description="Narratives and waves", guild_only=True
    )
    state_choices = [
        app_commands.Choice(name=v, value=v)
        for v in ("active", "watch", "surge", "alert", "closed")
    ]

    @group.command(name="list", description="List narratives (open ones by default)")
    @app_commands.choices(state=state_choices)
    async def n_list(
        interaction: discord.Interaction, state: app_commands.Choice[str] | None = None
    ) -> None:
        value = state.value if state else None
        await run_handler(interaction, lambda loc, uid: nar.narrative_list(gate, uid, loc, value))

    @group.command(name="show", description="Show a narrative")
    @app_commands.describe(narrative_id="Narrative id")
    async def n_show(interaction: discord.Interaction, narrative_id: str) -> None:
        await run_handler(
            interaction, lambda loc, uid: nar.narrative_show(gate, uid, loc, narrative_id)
        )

    @group.command(name="merge", description="Merge the source narrative into the target")
    @app_commands.describe(
        target="Narrative that absorbs", source="Narrative absorbed", reason="Why"
    )
    async def n_merge(
        interaction: discord.Interaction, target: str, source: str, reason: str
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: nar.narrative_merge(gate, uid, loc, target, source, reason),
        )

    @group.command(name="split", description="Move groups into a new narrative")
    @app_commands.describe(
        narrative_id="Narrative id", groups="Group ids, comma separated", reason="Why"
    )
    async def n_split(
        interaction: discord.Interaction, narrative_id: str, groups: str, reason: str
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: nar.narrative_split(gate, uid, loc, narrative_id, groups, reason),
        )

    @group.command(name="title", description="Validate the draft title, or set a corrected one")
    @app_commands.describe(narrative_id="Narrative id", title="Corrected title (optional)")
    async def n_title(
        interaction: discord.Interaction, narrative_id: str, title: str | None = None
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: nar.narrative_title(gate, uid, loc, narrative_id, title),
        )

    tree.add_command(group)
