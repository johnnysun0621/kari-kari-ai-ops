"""P0-2 任務查詢：藝人 /我的任務、管理端 /任務總覽。"""
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.scheduling_admin import is_admin
from services import repositories

logger = logging.getLogger(__name__)

# 顯示順序：藝人要行動的在前，資訊性的在後
SECTION_ORDER = [
    ("overdue", "🔴 已逾期"),
    ("needs_fix", "🛠 需修改"),
    ("due_soon", "⏰ 三天內截止"),
    ("open", "📋 進行中／待提交"),
    ("in_review", "📮 待審核（已提交，等公司確認）"),
]


def _trim_message(text: str, limit: int = 1900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 18].rstrip() + "\n...清單過長已截斷"


def _deadline_text(task: repositories.TaskItem, now: datetime) -> str:
    if not task.end:
        return "無截止日"
    day_diff = (task.end.date() - now.date()).days
    date_text = f"{task.end.month}/{task.end.day}"
    if task.end_has_time:
        date_text += task.end.strftime(" %H:%M")
    if day_diff < 0:
        return f"截止 {date_text}（已過 {-day_diff} 天）"
    if day_diff == 0:
        return f"截止 {date_text}（今天）"
    return f"截止 {date_text}（剩 {day_diff} 天）"


def _task_line(task: repositories.TaskItem, now: datetime, extra: str = "") -> str:
    type_text = task.task_type or "任務"
    line = f"・**{task.name}**｜{type_text}｜{_deadline_text(task, now)}"
    if extra:
        line += f"｜{extra}"
    return line


def format_task_buckets(
    buckets: dict[str, list[repositories.TaskItem]],
    now: datetime,
    name_of_artist: dict[str, str] | None = None,
) -> str:
    """把分區結果排成 Discord 訊息；name_of_artist 給管理端顯示藝人名。"""
    lines: list[str] = []
    for key, title in SECTION_ORDER:
        tasks = buckets.get(key, [])
        if not tasks:
            continue
        lines.append(f"**{title}**（{len(tasks)}）")
        for task in tasks:
            extra = ""
            if name_of_artist is not None:
                names = [name_of_artist.get(pid, "") for pid in task.artist_page_ids]
                names = [n for n in names if n]
                extra = "、".join(names) or "（未關聯藝人）"
            lines.append(_task_line(task, now, extra))
        lines.append("")
    return "\n".join(lines).strip()


class TasksCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="我的任務", description="查看我的未完成、即將到期與待審核任務")
    async def my_tasks(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return
        try:
            tasks = await repositories.get_discord_tasks_for_artist(artist.page_id)
        except Exception:
            logger.exception("讀取藝人任務失敗")
            await interaction.followup.send("❌ 讀取任務失敗，請稍後再試。", ephemeral=True)
            return
        now = datetime.now(config.get_tenant().tz)
        buckets = repositories.classify_tasks_for_display(tasks, now)
        body = format_task_buckets(buckets, now)
        if not body:
            await interaction.followup.send("🎉 目前沒有進行中的任務。", ephemeral=True)
            return
        await interaction.followup.send(
            _trim_message(f"📑 {artist.name} 的任務：\n\n{body}"), ephemeral=True
        )

    @app_commands.command(name="任務總覽", description="管理端：全部未結案任務（待審核／逾期／即將到期）")
    @is_admin()
    async def task_overview(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tasks = await repositories.get_active_tasks()
            artist_map = await repositories.get_artist_map()
        except Exception:
            logger.exception("讀取任務總覽失敗")
            await interaction.followup.send("❌ 讀取任務總覽失敗，請稍後再試。", ephemeral=True)
            return
        now = datetime.now(config.get_tenant().tz)
        buckets = repositories.classify_tasks_for_display(tasks, now)
        name_of = {pid: a.name for pid, a in artist_map.items()}
        body = format_task_buckets(buckets, now, name_of_artist=name_of)
        if not body:
            await interaction.followup.send("目前沒有未結案的任務。", ephemeral=True)
            return
        total = sum(len(v) for v in buckets.values())
        await interaction.followup.send(
            _trim_message(f"📋 未結案任務總覽（{total} 筆）：\n\n{body}"), ephemeral=True
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ 這個指令僅限管理者使用。"
        else:
            logger.exception("任務指令執行錯誤", exc_info=error)
            msg = "❌ 指令執行出錯，請稍後再試（詳見 bot log）。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TasksCog(bot))
