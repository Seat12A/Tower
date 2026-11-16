"""``/badge``: Seat 12A verification badges, affiliations, queue, organisation keys.

Persistent components (see ``handlers/badges.py`` for the custom ids). Actions that need
text (grant, revocation and block reasons, request decisions, key label) open a modal;
the others call gate directly.
"""

import functools
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Self, cast

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import Reply
from tower.intercom.handlers import badges as bh
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

SAMPLE_CUSTOM_IDS = (
    "t12a:bdg:show:x:12345",
    "t12a:bdga:validate:00000000-0000-0000-0000-000000000000",
    "t12a:bdgr:approve:00000000-0000-0000-0000-000000000000",
    "t12a:bdgp:resolve:00000000-0000-0000-0000-000000000000",
    "t12a:bdgq:requests",
    "t12a:bdgo:reports",
    "t12a:bdgk:x:12345",
)
"""One custom id per dynamic item class."""

_ACCOUNT = (
    r"t12a:bdg:(?P<action>show|grant|revoke|block|unblock|history|keys|newkey)"
    r":(?P<platform>[a-z]{1,16}):(?P<xid>.{1,70})"
)
_UUID = r"[0-9a-f-]{36}"
_AFF = rf"t12a:bdga:(?P<action>show|validate|reconfirm|reject|revoke|primary):(?P<id>{_UUID})"
_REQ = rf"t12a:bdgr:(?P<action>show|approve|reject):(?P<id>{_UUID})"
_REP = rf"t12a:bdgp:(?P<action>show|resolve|dismiss):(?P<id>{_UUID})"
_QUEUE = r"t12a:bdgq:(?P<view>requests|reports|affiliations)"
_OPEN = r"t12a:bdgo:(?P<view>requests|reports|affiliations)"
_KEYS = r"t12a:bdgk:(?P<platform>[a-z]{1,16}):(?P<xid>.{1,70})"

Platform = Literal["x", "bluesky", "threads", "mastodon", "youtube", "tiktok", "telegram", "web"]
Category = Literal["institutional", "media", "expert", "organization", "personality"]
View = Literal["requests", "reports", "affiliations"]
Handler = Callable[[str, int], Awaitable[Reply]]


def _values(interaction: discord.Interaction) -> list[str]:
    return list(cast(dict[str, Any], interaction.data or {}).get("values", []))


def _category_select(locale: str, default: str | None, *, keep: bool = False) -> ui.Select[Any]:
    options = []
    if keep:
        options.append(
            discord.SelectOption(
                label=t(locale, "badge.modal.keep")[:100], value="", default=default is None
            )
        )
    options += [
        discord.SelectOption(
            label=t(locale, f"badge.cat.{c}")[:100],
            value=c,
            description=bh.COLORS[c],
            default=c == default,
        )
        for c in bh.CATEGORIES
    ]
    return ui.Select(options=options)


@functools.cache
def _kit(ctx: Context) -> tuple[Any, ...]:
    gate = ctx.gate

    class TextModal(BaseModal):
        """One optional or required paragraph, then ``submit(text)``."""

        def __init__(
            self,
            locale: str,
            title_key: str,
            label_key: str,
            submit: Callable[[str | None], Handler],
            *,
            required: bool = False,
            help_key: str = "badge.modal.reason.help",
        ) -> None:
            super().__init__(title=t(locale, title_key)[:45])
            self.submit = submit
            self.text: ui.TextInput[Any] = ui.TextInput(
                style=discord.TextStyle.paragraph, max_length=1000, required=required
            )
            self.add_item(
                ui.Label(
                    text=t(locale, label_key)[:45],
                    description=t(locale, help_key)[:100],
                    component=self.text,
                )
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            value = self.text.value.strip() or None
            await run_handler(interaction, self.submit(value), edit=interaction.message is not None)

    class GrantModal(BaseModal):
        def __init__(
            self, locale: str, platform: str, xid: str, category: str | None = None
        ) -> None:
            super().__init__(title=t(locale, "badge.modal.grant.title")[:45])
            self.platform, self.xid = platform, xid
            self.category = _category_select(locale, category)
            self.label: ui.TextInput[Any] = ui.TextInput(max_length=120, required=False)
            self.logo: ui.TextInput[Any] = ui.TextInput(max_length=2048, required=False)
            self.note: ui.TextInput[Any] = ui.TextInput(
                style=discord.TextStyle.paragraph, max_length=2000, required=False
            )
            self.add_item(
                ui.TextDisplay(
                    f"-# {t(locale, 'badge.modal.grant.help', account=f'{platform}:{xid}')}"
                )
            )
            for key, component in (
                ("badge.modal.category", self.category),
                ("badge.modal.label", self.label),
                ("badge.modal.logo", self.logo),
                ("badge.modal.note", self.note),
            ):
                self.add_item(
                    ui.Label(
                        text=t(locale, key)[:45],
                        description=t(locale, key + ".help")[:100],
                        component=component,
                    )
                )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            category = self.category.values[0] if self.category.values else bh.CATEGORIES[-1]
            label = self.label.value.strip() or None
            logo = self.logo.value.strip() or None
            note = self.note.value.strip() or None
            await run_handler(
                interaction,
                lambda loc, uid: bh.badge_grant(
                    gate, uid, loc, self.platform, self.xid, category, label, logo, note
                ),
                edit=interaction.message is not None,
            )

    class ApproveModal(BaseModal):
        def __init__(self, locale: str, request_id: str) -> None:
            super().__init__(title=t(locale, "badge.modal.approve.title")[:45])
            self.request_id = request_id
            self.category = _category_select(locale, None, keep=True)
            self.note: ui.TextInput[Any] = ui.TextInput(
                style=discord.TextStyle.paragraph, max_length=1000, required=False
            )
            self.add_item(
                ui.Label(
                    text=t(locale, "badge.modal.category")[:45],
                    description=t(locale, "badge.modal.approve.category.help")[:100],
                    component=self.category,
                )
            )
            self.add_item(
                ui.Label(
                    text=t(locale, "badge.modal.note")[:45],
                    description=t(locale, "badge.modal.reason.help")[:100],
                    component=self.note,
                )
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            category = (self.category.values[0] if self.category.values else "") or None
            note = self.note.value.strip() or None
            await run_handler(
                interaction,
                lambda loc, uid: bh.request_decide(
                    gate, uid, loc, self.request_id, True, category, note
                ),
                edit=interaction.message is not None,
            )

    def reason_modal(
        locale: str, title_key: str, submit: Callable[[str | None], Handler], **kw: Any
    ) -> TextModal:
        return TextModal(locale, title_key, "badge.modal.reason", submit, **kw)

    class AccountButton(ui.DynamicItem[ui.Button[Any]], template=_ACCOUNT):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, action, platform, xid = (self.item.custom_id or "").split(":", 4)
            locale = interaction_locale(interaction)
            if action == "grant":
                await interaction.response.send_modal(GrantModal(locale, platform, xid))
            elif action in ("revoke", "block"):
                await interaction.response.send_modal(
                    reason_modal(
                        locale,
                        f"badge.modal.{action}.title",
                        lambda reason: (
                            lambda loc, uid: bh.badge_end(
                                gate, uid, loc, platform, xid, action, reason
                            )
                        ),
                    )
                )
            elif action == "newkey":
                await interaction.response.send_modal(
                    TextModal(
                        locale,
                        "badge.modal.key.title",
                        "badge.modal.key.label",
                        lambda label: (
                            lambda loc, uid: bh.org_key_create(gate, uid, loc, platform, xid, label)
                        ),
                        help_key="badge.modal.key.help",
                    )
                )
            else:
                calls: dict[str, Handler] = {
                    "unblock": lambda loc, uid: bh.badge_end(
                        gate, uid, loc, platform, xid, "unblock"
                    ),
                    "history": lambda loc, uid: bh.badge_history(gate, uid, loc, platform, xid),
                    "keys": lambda loc, uid: bh.org_keys(gate, uid, loc, platform, xid),
                }
                call = calls.get(
                    action, lambda loc, uid: bh.badge_show(gate, uid, loc, platform, xid)
                )
                await run_handler(interaction, call, edit=True)

    class AffiliationButton(ui.DynamicItem[ui.Button[Any]], template=_AFF):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, action, pid = (self.item.custom_id or "").split(":", 3)
            locale = interaction_locale(interaction)
            if action in ("reject", "revoke"):
                await interaction.response.send_modal(
                    reason_modal(
                        locale,
                        f"badge.modal.aff_{action}.title",
                        lambda reason: (
                            lambda loc, uid: bh.affiliation_act(gate, uid, loc, pid, action, reason)
                        ),
                    )
                )
            elif action == "show":
                await run_handler(
                    interaction,
                    lambda loc, uid: bh.affiliation_show(gate, uid, loc, pid),
                    edit=True,
                )
            else:
                await run_handler(
                    interaction,
                    lambda loc, uid: bh.affiliation_act(gate, uid, loc, pid, action),
                    edit=True,
                )

    class RequestButton(ui.DynamicItem[ui.Button[Any]], template=_REQ):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, action, pid = (self.item.custom_id or "").split(":", 3)
            locale = interaction_locale(interaction)
            if action == "approve":
                await interaction.response.send_modal(ApproveModal(locale, pid))
            elif action == "reject":
                await interaction.response.send_modal(
                    TextModal(
                        locale,
                        "badge.modal.req_reject.title",
                        "badge.modal.note",
                        lambda note: (
                            lambda loc, uid: bh.request_decide(
                                gate, uid, loc, pid, False, None, note
                            )
                        ),
                    )
                )
            else:
                await run_handler(
                    interaction, lambda loc, uid: bh.request_show(gate, uid, loc, pid), edit=True
                )

    class ReportButton(ui.DynamicItem[ui.Button[Any]], template=_REP):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, action, pid = (self.item.custom_id or "").split(":", 3)
            if action == "show":
                call: Handler = lambda loc, uid: bh.report_show(gate, uid, loc, pid)  # noqa: E731
            else:
                status = "resolved" if action == "resolve" else "dismissed"
                call = lambda loc, uid: bh.report_resolve(gate, uid, loc, pid, status)  # noqa: E731
            await run_handler(interaction, call, edit=True)

    class QueueButton(ui.DynamicItem[ui.Button[Any]], template=_QUEUE):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            view = (self.item.custom_id or "").rsplit(":", 1)[-1]
            await run_handler(
                interaction, lambda loc, uid: bh.queue(gate, uid, loc, view), edit=True
            )

    class OpenSelect(ui.DynamicItem[ui.Select[Any]], template=_OPEN):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Select(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            view = (self.item.custom_id or "").rsplit(":", 1)[-1]
            values = _values(interaction)
            if not values:
                return
            pid = values[0]
            shows: dict[str, Handler] = {
                "requests": lambda loc, uid: bh.request_show(gate, uid, loc, pid),
                "reports": lambda loc, uid: bh.report_show(gate, uid, loc, pid),
                "affiliations": lambda loc, uid: bh.affiliation_show(gate, uid, loc, pid),
            }
            await run_handler(interaction, shows[view], edit=True)

    class KeySelect(ui.DynamicItem[ui.Select[Any]], template=_KEYS):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Select(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, platform, xid = (self.item.custom_id or "").split(":", 3)
            values = _values(interaction)
            if not values:
                return
            key = values[0]
            await run_handler(
                interaction,
                lambda loc, uid: bh.org_key_revoke(gate, uid, loc, platform, xid, key),
                edit=True,
            )

    return (
        (
            AccountButton,
            AffiliationButton,
            RequestButton,
            ReportButton,
            QueueButton,
            OpenSelect,
            KeySelect,
        ),
        GrantModal,
        reason_modal,
    )


def dynamic_items(ctx: Context) -> list[type]:
    return list(_kit(ctx)[0])


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    _, grant_modal, reason_modal = _kit(ctx)
    group = app_commands.Group(
        name="badge", description="Seat 12A verification badges", guild_only=True
    )
    account_help = "Native account id (X numeric id, never a handle)"

    @group.command(name="show", description="Badge, status and affiliations of an account")
    @app_commands.describe(account=account_help, platform="Platform (default: x)")
    async def b_show(
        interaction: discord.Interaction, account: str, platform: Platform = "x"
    ) -> None:
        await run_handler(
            interaction, lambda loc, uid: bh.badge_show(gate, uid, loc, platform, account)
        )

    @group.command(name="grant", description="Grant or edit a badge (editors)")
    @app_commands.describe(account=account_help, category="Badge category", platform="Platform")
    async def b_grant(
        interaction: discord.Interaction,
        account: str,
        category: Category,
        platform: Platform = "x",
    ) -> None:
        locale = interaction_locale(interaction)
        checked = bh.check_account(locale, platform, account)
        if isinstance(checked, Reply):
            await run_handler(interaction, lambda loc, uid: _same(checked))
            return
        await interaction.response.send_modal(grant_modal(locale, *checked, category))

    async def _end_with_reason(
        interaction: discord.Interaction, account: str, platform: str, action: str
    ) -> None:
        locale = interaction_locale(interaction)
        checked = bh.check_account(locale, platform, account)
        if isinstance(checked, Reply):
            await run_handler(interaction, lambda loc, uid: _same(checked))
            return
        plat, xid = checked
        await interaction.response.send_modal(
            reason_modal(
                locale,
                f"badge.modal.{action}.title",
                lambda reason: (
                    lambda loc, uid: bh.badge_end(gate, uid, loc, plat, xid, action, reason)
                ),
            )
        )

    @group.command(name="revoke", description="Revoke a badge (editors)")
    @app_commands.describe(account=account_help, platform="Platform")
    async def b_revoke(
        interaction: discord.Interaction, account: str, platform: Platform = "x"
    ) -> None:
        await _end_with_reason(interaction, account, platform, "revoke")

    @group.command(name="block", description="Block an account from any badge (editors)")
    @app_commands.describe(account=account_help, platform="Platform")
    async def b_block(
        interaction: discord.Interaction, account: str, platform: Platform = "x"
    ) -> None:
        await _end_with_reason(interaction, account, platform, "block")

    @group.command(name="unblock", description="Lift a block (editors)")
    @app_commands.describe(account=account_help, platform="Platform")
    async def b_unblock(
        interaction: discord.Interaction, account: str, platform: Platform = "x"
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: bh.badge_end(gate, uid, loc, platform, account, "unblock"),
        )

    @group.command(name="history", description="Badge and affiliation history of an account")
    @app_commands.describe(account=account_help, platform="Platform")
    async def b_history(
        interaction: discord.Interaction, account: str, platform: Platform = "x"
    ) -> None:
        await run_handler(
            interaction, lambda loc, uid: bh.badge_history(gate, uid, loc, platform, account)
        )

    @group.command(name="affiliate", description="Affiliate an account to a badged organisation")
    @app_commands.describe(
        member="Native id of the individual account",
        org="Native id of the organisation account (organisation, media or institutional)",
        title="Role shown (e.g. Journalist)",
        pending="Leave it pending instead of validating it now",
        platform="Platform of both accounts",
    )
    async def b_affiliate(
        interaction: discord.Interaction,
        member: str,
        org: str,
        title: str | None = None,
        pending: bool = False,
        platform: Platform = "x",
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: bh.affiliate(gate, uid, loc, platform, member, org, title, pending),
        )

    @group.command(name="affiliation", description="Show an affiliation (short id accepted)")
    @app_commands.describe(id="Affiliation id (6 characters or more)")
    async def b_affiliation(interaction: discord.Interaction, id: str) -> None:
        await run_handler(interaction, lambda loc, uid: bh.affiliation_show(gate, uid, loc, id))

    @group.command(name="queue", description="Pending requests, open reports, pending affiliations")
    @app_commands.describe(view="What to review (default: requests)")
    async def b_queue(interaction: discord.Interaction, view: View = "requests") -> None:
        await run_handler(interaction, lambda loc, uid: bh.queue(gate, uid, loc, view))

    @group.command(name="request", description="Show a badge request (short id accepted)")
    @app_commands.describe(id="Request id (6 characters or more)")
    async def b_request(interaction: discord.Interaction, id: str) -> None:
        await run_handler(interaction, lambda loc, uid: bh.request_show(gate, uid, loc, id))

    @group.command(name="report", description="Show a badge report (short id accepted)")
    @app_commands.describe(id="Report id (6 characters or more)")
    async def b_report(interaction: discord.Interaction, id: str) -> None:
        await run_handler(interaction, lambda loc, uid: bh.report_show(gate, uid, loc, id))

    @group.command(name="org-keys", description="Organisation keys of a badged organisation")
    @app_commands.describe(account="Native id of the organisation account", platform="Platform")
    async def b_keys(
        interaction: discord.Interaction, account: str, platform: Platform = "x"
    ) -> None:
        await run_handler(
            interaction, lambda loc, uid: bh.org_keys(gate, uid, loc, platform, account)
        )

    @group.command(name="import", description="Start a Wikidata import, or show the last ones")
    @app_commands.describe(
        scopes="Country QIDs or intl, space separated (empty: configured scopes)",
        start="Start an import now (default: only show the status)",
    )
    async def b_import(
        interaction: discord.Interaction, scopes: str | None = None, start: bool = False
    ) -> None:
        if start:
            await run_handler(
                interaction, lambda loc, uid: bh.badge_start_import(gate, uid, loc, scopes)
            )
        else:
            await run_handler(interaction, lambda loc, uid: bh.badge_imports(gate, uid, loc))

    tree.add_command(group)


async def _same(reply: Reply) -> Reply:
    return reply
