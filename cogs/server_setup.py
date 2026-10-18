"""可平轉的 Discord 頻道與身分組初始化。"""
import logging
import asyncio
from dataclasses import dataclass
from urllib.parse import urlencode

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.permissions import is_admin

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    env_name: str
    topic: str
    read_only: bool = False
    admin_only: bool = False


LEGACY_CHANNEL_NAMES: dict[str, tuple[str, ...]] = {
    # Existing KariKari servers used these names before channel initialization
    # was introduced. Keep their history and IDs instead of creating duplicates.
    "任務查詢入口": ("任務專用",),
    "營運內部通知": ("營運內部專用", "營運內部"),
    "時間管理": ("時間資訊彙整",),
}


def channel_specs() -> tuple[ChannelSpec, ...]:
    return (
        ChannelSpec("公告區", "ANNOUNCE_CHANNEL_ID", "公開類型 Task、時間收集與 Notion 布告欄", read_only=True),
        ChannelSpec("時間管理", "SCHEDULE_CHANNEL_ID", "藝人月度可配合時間回報與管理"),
        ChannelSpec("任務查詢入口", "TASK_CHANNEL_ID", "藝人查詢任務與提交作業"),
        ChannelSpec("課程助教", "AI_CHANNEL_ID", "藝人問小律查自己的任務（私人討論串）"),
        ChannelSpec("營運內部通知", "ADMIN_NOTIFY_CHANNEL_ID", "Bot 自動推送任務、審核、排課與 SLA 通知", admin_only=True),
        ChannelSpec("營運操作", "ADMIN_COMMAND_CHANNEL_ID", "控方 slash command 操作入口", admin_only=True),
    )


def env_summary(guild_id: int, admin_role_id: int, channel_ids: dict[str, int]) -> str:
    values = {
        "GUILD_ID": guild_id,
        "ADMIN_ROLE_ID": admin_role_id,
        **{spec.env_name: channel_ids[spec.name] for spec in channel_specs()},
        "BULLETIN_CHANNEL_ID": channel_ids["公告區"],
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


def command_channel_groups() -> dict[str, tuple[str, bool]]:
    """Return command name -> (channel name, requires 控方 role)."""
    return {
        "時間資訊蒐集": ("營運操作", True),
        "管理可配合時間": ("時間管理", False),
        "我的任務": ("任務查詢入口", False),
        "時間交集查詢": ("營運操作", True),
        "取消時間蒐集": ("營運操作", True),
        "從時間池建立任務": ("營運操作", True),
        "任務總覽": ("營運操作", True),
        "任務通知重送": ("營運操作", True),
        "審核提交": ("營運操作", True),
        "布告欄同步": ("營運操作", True),
        "伺服器初始化": ("營運操作", True),
    }


def command_permission_entries(guild_id: int, channel_id: int) -> list[dict]:
    """Allow a command only in one channel; command default permissions handle roles."""
    return [
        {
            "id": str(guild_id - 1),
            "type": 3,  # All Channels
            "permission": False,
        },
        {
            "id": str(channel_id),
            "type": 3,  # CHANNEL
            "permission": True,
        },
    ]


def find_existing_channel(guild: discord.Guild, spec: ChannelSpec):
    """Find a configured channel, then canonical or legacy name.

    A configured ID is the tenant's source of truth. This is important during
    migration: an existing channel may still have an old display name such as
    ``任務專用`` while its ID is already configured in ``TASK_CHANNEL_ID``.
    """
    configured_id = getattr(config, spec.env_name, 0)
    if configured_id:
        channel = guild.get_channel(configured_id)
        if channel is not None and channel in guild.text_channels:
            return channel

    names = (spec.name, *LEGACY_CHANNEL_NAMES.get(spec.name, ()))
    for name in names:
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel is not None:
            return channel
    return None


async def sync_command_permissions(
    bot: commands.Bot,
    guild: discord.Guild,
    channel_ids: dict[str, int],
    admin_role_id: int,
    access_token: str,
) -> None:
    """Use an authorized Discord user token to update command visibility."""
    import httpx

    application_commands = await bot.tree.fetch_commands(guild=guild)
    groups = command_channel_groups()
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(base_url="https://discord.com/api/v10", headers=headers) as client:
        for command in application_commands:
            group = groups.get(command.name)
            if group is None:
                continue
            channel_name, _requires_admin = group
            permissions = command_permission_entries(
                guild.id,
                channel_ids[channel_name],
            )
            url = f"/applications/{bot.user.id}/guilds/{guild.id}/commands/{command.id}/permissions"
            for attempt in range(4):
                response = await client.put(url, json={"permissions": permissions})
                if response.status_code != 429:
                    response.raise_for_status()
                    break
                if attempt == 3:
                    response.raise_for_status()
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except (ValueError, TypeError):
                    retry_after = 1.0
                await asyncio.sleep(min(max(retry_after, 0.5), 10.0))


def oauth_authorize_url(state: str) -> str:
    tenant = config.get_tenant()
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": tenant.discord_client_id,
        "response_type": "code",
        "redirect_uri": tenant.discord_oauth_redirect_uri,
        "scope": "identify applications.commands.permissions.update",
        "state": state,
    })


def _overwrites(
    guild: discord.Guild,
    admin_role: discord.Role,
    artist_role: discord.Role,
    bot_member: discord.Member,
    spec: ChannelSpec,
) -> dict:
    admin = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        use_application_commands=True,
        manage_messages=True,
        create_private_threads=True,
        send_messages_in_threads=True,
    )
    bot = discord.PermissionOverwrite(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        use_application_commands=True,
        manage_messages=True,
        manage_threads=True,
        create_private_threads=True,
        send_messages_in_threads=True,
    )
    if spec.admin_only:
        everyone = discord.PermissionOverwrite(view_channel=False)
        artist = discord.PermissionOverwrite(view_channel=False)
    else:
        everyone = discord.PermissionOverwrite(
            view_channel=False,
        )
        artist = discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=not spec.read_only,
            use_application_commands=True,
            create_private_threads=spec.name == "課程助教",
            send_messages_in_threads=spec.name == "課程助教",
        )
    return {
        guild.default_role: everyone,
        artist_role: artist,
        admin_role: admin,
        bot_member: bot,
    }


async def initialize_guild(guild: discord.Guild, bot_member: discord.Member) -> tuple[discord.Role, dict[str, int]]:
    tenant = config.get_tenant()
    reason = f"{tenant.name} AI Ops 初始化"
    admin_role = guild.get_role(tenant.admin_role_id) if tenant.admin_role_id else None
    admin_role = admin_role or discord.utils.get(guild.roles, name="控方")
    if admin_role is None:
        admin_role = await guild.create_role(name="控方", reason=reason)
    artist_role = discord.utils.get(guild.roles, name="藝人")
    if artist_role is None:
        artist_role = await guild.create_role(name="藝人", reason=reason)

    category_name = f"{tenant.name} 營運"
    category = discord.utils.get(guild.categories, name=category_name)
    if category is None:
        category = await guild.create_category(category_name, reason=reason)

    channel_ids: dict[str, int] = {}
    for spec in channel_specs():
        channel = find_existing_channel(guild, spec)
        if channel is None and spec.name == "營運內部通知":
            # Preserve the existing channel ID and message history during the rename.
            channel = discord.utils.get(guild.text_channels, name="營運內部")
            if channel is not None:
                await channel.edit(name=spec.name, reason=reason)
        if channel is not None and channel.name != spec.name:
            # Configured IDs are authoritative. Rename legacy channels in place so
            # migration keeps the channel history and does not create duplicates.
            await channel.edit(name=spec.name, reason=reason)
        overwrites = _overwrites(guild, admin_role, artist_role, bot_member, spec)
        if channel is None:
            channel = await guild.create_text_channel(
                spec.name,
                category=category,
                topic=spec.topic,
                overwrites=overwrites,
                reason=reason,
            )
        else:
            await channel.edit(
                category=category,
                topic=spec.topic,
                overwrites=overwrites,
                reason=reason,
            )
        channel_ids[spec.name] = channel.id
    return admin_role, channel_ids


class ServerSetupConfirmView(discord.ui.View):
    def __init__(self, requester_id: int):
        super().__init__(timeout=300)
        self.requester_id = requester_id

    @discord.ui.button(label="確認初始化", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ 只有發起初始化的控方可以確認。", ephemeral=True)
            return
        if interaction.guild is None or interaction.guild.me is None:
            await interaction.response.send_message("❌ 只能在 Discord 伺服器內初始化。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            admin_role, channel_ids = await initialize_guild(interaction.guild, interaction.guild.me)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Bot 缺少「管理頻道」或「管理身分組」權限。補上後再試。",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Discord 伺服器初始化失敗")
            await interaction.followup.send("❌ Discord 初始化失敗，請稍後再試。", ephemeral=True)
            return
        tenant = config.get_tenant()
        if not tenant.discord_client_id or not tenant.discord_client_secret or not tenant.discord_oauth_redirect_uri:
            await interaction.followup.send(
                "⚠️ 頻道與身分組已完成，但尚未設定 Discord OAuth。"
                "請補上 DISCORD_CLIENT_ID、DISCORD_CLIENT_SECRET、DISCORD_OAUTH_REDIRECT_URI 後再試。",
                ephemeral=True,
            )
            self.stop()
            return
        import secrets
        import time
        state = secrets.token_urlsafe(32)
        interaction.client.discord_oauth_states[state] = {
            "expires_at": time.time() + 600,
            "requester_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_ids": channel_ids,
            "admin_role_id": admin_role.id,
        }
        summary = env_summary(interaction.guild.id, admin_role.id, channel_ids)
        await interaction.followup.send(
            "✅ 頻道與身分組已完成。請開啟以下連結完成一次 Discord 授權，"
            "授權後 Bot 會自動設定各頻道的 slash command 可見範圍：\n"
            f"{oauth_authorize_url(state)}\n\n"
            "授權連結 10 分鐘內有效。\n\n"
            "目前租戶設定：\n"
            f"```env\n{summary}\n```\n"
            "完成授權後，各頻道只會顯示對應的 slash command。",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ 只有發起初始化的控方可以取消。", ephemeral=True)
            return
        await interaction.response.edit_message(
            content="已取消伺服器初始化，Discord 設定未變更。",
            view=None,
        )
        self.stop()


class ServerSetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="伺服器初始化", description="管理端：建立或對齊標準頻道與身分組")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def initialize_server(self, interaction: discord.Interaction):
        preview = "\n".join(
            f"・{item.name}：{'控方限定' if item.admin_only else '唯讀公告' if item.read_only else '一般操作'}"
            for item in channel_specs()
        )
        await interaction.response.send_message(
            "即將建立或更新以下用途頻道，並建立／沿用 `控方`、`藝人` 身分組：\n"
            f"{preview}\n\n既有同名頻道會更新分類、主題與權限；確認後才執行。",
            view=ServerSetupConfirmView(interaction.user.id),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerSetupCog(bot))
