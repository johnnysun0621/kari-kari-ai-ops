"""Notion 營運布告欄同步到 Discord 公告區。"""
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from services import repositories
from services.permissions import is_admin

logger = logging.getLogger(__name__)
_STATE_PATH = Path(".state/bulletin_sync.json")


def bulletin_channel_id() -> int:
    return config.BULLETIN_CHANNEL_ID or config.ANNOUNCE_CHANNEL_ID or config.SCHEDULE_CHANNEL_ID


def bulletin_fingerprint(item: repositories.BulletinItem) -> str:
    raw = "|".join([
        item.title, item.category, item.status, item.date.isoformat() if item.date else "",
        ",".join(item.people), str(item.pinned), item.url,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def format_bulletin(item: repositories.BulletinItem) -> str:
    """產生不超過 Discord 訊息限制的布告內容。"""
    pin = "📌 " if item.pinned else ""
    date_text = item.date.strftime("%Y/%m/%d %H:%M") if item.date else "未填日期"
    people = "、".join(item.people) if item.people else "未指定"
    lines = [
        f"{pin}**{item.title}**",
        f"類別：{item.category}｜狀態：{item.status}",
        f"日期：{date_text}｜人員：{people}",
    ]
    if item.url:
        lines.append(f"[開啟 Notion 頁面]({item.url})")
    return "\n".join(lines)[:1900]


def _load_state() -> dict[str, dict[str, str]]:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, dict[str, str]]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


class BulletinCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()

    async def sync_bulletins(self) -> tuple[int, int, int]:
        channel = self.bot.get_channel(bulletin_channel_id())
        if channel is None:
            raise RuntimeError("找不到布告欄同步頻道，請設定 BULLETIN_CHANNEL_ID 或 ANNOUNCE_CHANNEL_ID")
        items = await repositories.get_bulletin_items()
        state = _load_state()
        created = updated = skipped = 0
        for item in items:
            fingerprint = bulletin_fingerprint(item)
            record = state.get(item.page_id, {})
            if record.get("fingerprint") == fingerprint:
                skipped += 1
                continue
            message = None
            message_id = record.get("message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                except (discord.NotFound, discord.HTTPException, ValueError):
                    message = None
            embed = discord.Embed(
                description=format_bulletin(item),
                color=discord.Color.blurple(),
                timestamp=datetime.now(config.get_tenant().tz),
            )
            if message is None:
                message = await channel.send(embed=embed)
                created += 1
            else:
                await message.edit(embed=embed)
                updated += 1
            state[item.page_id] = {
                "message_id": str(message.id),
                "fingerprint": fingerprint,
            }
        _save_state(state)
        return created, updated, skipped

    @tasks.loop(minutes=config.BULLETIN_SYNC_INTERVAL_MINUTES)
    async def sync_loop(self):
        try:
            await self.sync_bulletins()
        except Exception:
            logger.exception("Notion 布告欄自動同步失敗")

    @sync_loop.before_loop
    async def before_sync_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="布告欄同步", description="管理端：同步 Notion 布告欄到 Discord 公告區")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def sync_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            created, updated, skipped = await self.sync_bulletins()
        except Exception:
            logger.exception("手動同步 Notion 布告欄失敗")
            await interaction.followup.send(
                "❌ 布告欄同步失敗，請確認 Notion integration 已分享該布告欄，" 
                "以及 Discord 公告區權限。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 布告欄同步完成：新增 {created}、更新 {updated}、未變更 {skipped}。",
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("布告欄指令執行錯誤", exc_info=error)
        message = "❌ 布告欄指令執行失敗，請稍後再試。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BulletinCog(bot))
