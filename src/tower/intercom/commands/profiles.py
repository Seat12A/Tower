"""``/profile`` and ``/level``: cards with persistent buttons, long reasons in modals.

- ``t12a:prof:<show|history|set|rollback>``: profile card navigation; ``set`` and
  ``rollback`` open a form (path / value / reason, reason).
- ``t12a:lvl:<list|set>`` and select ``t12a:lvl:pick``: overrides list; ``set`` opens a form
  (target type radio, target, level select, hours, reason); picking an override opens the
  revoke reason form.
"""

import functools
import re
from typing import Any, Self, cast

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import profiles as ph
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

SAMPLE_CUSTOM_IDS = (ph.CID_PROFILE_SHOW, ph.CID_LEVEL_LIST, ph.CID_LEVEL_PICK)

_PROFILE_BUTTON = r"t12a:prof:(?P<action>show|history|set|rollback)"
_LEVEL_BUTTON = r"t12a:lvl:(?P<action>list|set)"
_LEVEL_SELECT = re.escape(ph.CID_LEVEL_PICK)


def _reason_input() -> ui.TextInput[Any]:
    return ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000)


def _label(locale: str, key: str, component: Any, *, help_: bool = True) -> ui.Label[Any]:
    return ui.Label(
        text=t(locale, key)[:45],
        description=(t(locale, f"{key}.help")[:100] or None) if help_ else None,
        component=component,
    )


@functools.cache
def _kit(ctx: Context) -> tuple[Any, ...]:
    gate = ctx.gate

    class ProfileSetModal(BaseModal):
        def __init__(self, locale: str) -> None:
            super().__init__(title=t(locale, "profile.modal.set.title")[:45])
            self.path: ui.TextInput[Any] = ui.TextInput(
                max_length=100, placeholder="levels.N1.e1_threshold"
            )
            self.value: ui.TextInput[Any] = ui.TextInput(max_length=200, placeholder="0.2")
            self.reason = _reason_input()
            self.add_item(_label(locale, "profile.modal.path", self.path))
            self.add_item(_label(locale, "profile.modal.value", self.value))
            self.add_item(_label(locale, "profile.modal.reason", self.reason))

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: ph.set_value(
                    gate, uid, loc, self.path.value, self.value.value, self.reason.value
                ),
                edit=interaction.message is not None,
            )

    class RollbackModal(BaseModal):
        def __init__(self, locale: str) -> None:
            super().__init__(title=t(locale, "profile.modal.rollback.title")[:45])
            self.reason = _reason_input()
            self.add_item(ui.TextDisplay(f"-# {t(locale, 'profile.modal.rollback.help')}"))
            self.add_item(_label(locale, "profile.modal.reason", self.reason))

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: ph.rollback(gate, uid, loc, self.reason.value),
                edit=interaction.message is not None,
            )

    class LevelSetModal(BaseModal):
        def __init__(self, locale: str) -> None:
            super().__init__(title=t(locale, "level.modal.set.title")[:45])
            self.target_type: ui.RadioGroup[Any] = ui.RadioGroup(
                options=[
                    discord.RadioGroupOption(
                        label=t(locale, f"level.target.{kind}"),
                        value=kind,
                        default=kind == "content",
                    )
                    for kind in ph.TARGET_TYPES
                ]
            )
            self.target: ui.TextInput[Any] = ui.TextInput(max_length=200)
            self.level: ui.Select[Any] = ui.Select(
                options=[discord.SelectOption(label=lv, value=lv) for lv in ph.LEVELS]
            )
            self.hours: ui.TextInput[Any] = ui.TextInput(default="24", max_length=6)
            self.reason = _reason_input()
            self.add_item(_label(locale, "level.modal.type", self.target_type, help_=False))
            self.add_item(_label(locale, "level.modal.target", self.target))
            self.add_item(_label(locale, "level.modal.level", self.level, help_=False))
            self.add_item(_label(locale, "level.modal.hours", self.hours))
            self.add_item(_label(locale, "level.modal.reason", self.reason))

        async def on_submit(self, interaction: discord.Interaction) -> None:
            level = self.level.values[0] if self.level.values else ph.LEVELS[0]
            await run_handler(
                interaction,
                lambda loc, uid: ph.level_set_form(
                    gate,
                    uid,
                    loc,
                    self.target_type.value or "content",
                    self.target.value,
                    level,
                    self.hours.value,
                    self.reason.value,
                ),
                edit=interaction.message is not None,
            )

    class RevokeModal(BaseModal):
        def __init__(self, override_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "level.modal.revoke.title")[:45])
            self.override_id = override_id
            self.reason = _reason_input()
            self.add_item(_label(locale, "level.modal.reason", self.reason))

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: ph.level_revoke(
                    gate, uid, loc, self.override_id, self.reason.value
                ),
                edit=interaction.message is not None,
            )

    class ProfileButton(ui.DynamicItem[ui.Button[Any]], template=_PROFILE_BUTTON):
        def __init__(self, action: str) -> None:
            super().__init__(ui.Button(custom_id=f"t12a:prof:{action}"))
            self.action = action

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["action"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            locale = interaction_locale(interaction)
            if self.action == "set":
                await interaction.response.send_modal(ProfileSetModal(locale))
            elif self.action == "rollback":
                await interaction.response.send_modal(RollbackModal(locale))
            elif self.action == "history":
                await run_handler(
                    interaction, lambda loc, uid: ph.history(gate, uid, loc), edit=True
                )
            else:
                await run_handler(interaction, lambda loc, uid: ph.show(gate, uid, loc), edit=True)

    class LevelButton(ui.DynamicItem[ui.Button[Any]], template=_LEVEL_BUTTON):
        def __init__(self, action: str) -> None:
            super().__init__(ui.Button(custom_id=f"t12a:lvl:{action}"))
            self.action = action

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["action"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            if self.action == "set":
                await interaction.response.send_modal(
                    LevelSetModal(interaction_locale(interaction))
                )
            else:
                await run_handler(
                    interaction, lambda loc, uid: ph.level_list(gate, uid, loc), edit=True
                )

    class LevelSelect(ui.DynamicItem[ui.Select[Any]], template=_LEVEL_SELECT):
        def __init__(self) -> None:
            super().__init__(ui.Select(custom_id=ph.CID_LEVEL_PICK))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            values = list(cast(dict[str, Any], interaction.data or {}).get("values", []))
            if values:
                await interaction.response.send_modal(
                    RevokeModal(str(values[0]), interaction_locale(interaction))
                )

    return (
        ProfileButton,
        LevelButton,
        LevelSelect,
        ProfileSetModal,
        RollbackModal,
        LevelSetModal,
        RevokeModal,
    )


def dynamic_items(ctx: Context) -> list[type]:
    return list(_kit(ctx)[:3])


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    _, _, _, set_modal, rollback_modal, level_modal, revoke_modal = _kit(ctx)

    prof = app_commands.Group(name="profile", description="Processing profiles", guild_only=True)

    @prof.command(name="show", description="Show the active processing profile")
    async def p_show(interaction: discord.Interaction) -> None:
        await run_handler(interaction, lambda loc, uid: ph.show(gate, uid, loc))

    @prof.command(name="history", description="List the profile versions")
    async def p_history(interaction: discord.Interaction) -> None:
        await run_handler(interaction, lambda loc, uid: ph.history(gate, uid, loc))

    @prof.command(name="set", description="Change one setting (new version, activated)")
    async def p_set(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(set_modal(interaction_locale(interaction)))

    @prof.command(name="rollback", description="Re-activate the previous profile version")
    async def p_rollback(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(rollback_modal(interaction_locale(interaction)))

    lvl = app_commands.Group(name="level", description="Manual level overrides", guild_only=True)

    @lvl.command(name="set", description="Force a level for a content, group or account")
    async def l_set(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(level_modal(interaction_locale(interaction)))

    @lvl.command(name="list", description="List the active level overrides")
    async def l_list(interaction: discord.Interaction) -> None:
        await run_handler(interaction, lambda loc, uid: ph.level_list(gate, uid, loc))

    @lvl.command(name="revoke", description="End a level override (opens a reason form)")
    @app_commands.describe(override_id="Override id (at least 6 characters)")
    async def l_revoke(interaction: discord.Interaction, override_id: str) -> None:
        await interaction.response.send_modal(
            revoke_modal(override_id, interaction_locale(interaction))
        )

    tree.add_command(prof)
    tree.add_command(lvl)
