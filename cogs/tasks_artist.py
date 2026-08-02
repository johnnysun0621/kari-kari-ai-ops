"""P0-2 任務查詢：藝人 /我的任務、管理端 /任務總覽。"""
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard, ui_text
from services.permissions import is_admin
from services import audit, repositories, task_notifications

logger = logging.getLogger(__name__)


class TaskReceiptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"task_receipt:(?P<task_id>[\w-]+)"):
    """任務通知中的持久化收到按鈕。"""

    def __init__(self, task_id: str):
        super().__init__(
            discord.ui.Button(
                label="收到任務",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"task_receipt:{task_id}",
            )
        )
        self.task_id = task_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match,
    ) -> "TaskReceiptButton":
        """從持久化按鈕的 custom_id 還原任務 ID。"""
        return cls(match.group("task_id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        # 先回應 Discord，再進行 Notion 查詢，避免超過 3 秒互動期限。
        await interaction.response.defer(ephemeral=True, thinking=True)
        task = await repositories.get_task(self.task_id)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if task is None or artist is None or artist.page_id not in task.artist_page_ids:
            await interaction.followup.send("❌ 這不是分派給你的任務。", ephemeral=True)
            return
        if not task_notifications.can_acknowledge(task):
            await interaction.followup.send(
                f"ℹ️ 這項任務目前狀態是「{task.status or '未設定'}」，不需要重複確認。",
                ephemeral=True,
            )
            return

        received_at = datetime.now(config.get_tenant().tz)
        try:
            stored_in_notion = await repositories.acknowledge_task(self.task_id, received_at)
            await audit.log_event(
                event_name=f"藝人收到任務｜{task.name}",
                action_type="更新",
                operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Tasks",
                target_url=task.url,
                after=(
                    f"狀態：進行中｜收到時間：{received_at.isoformat()}"
                    + ("｜Notion 已寫入" if stored_in_notion else "｜收到時間欄位尚未建立")
                ),
            )
        except Exception:
            logger.exception("任務收到確認寫入失敗：%s", self.task_id)
            await interaction.followup.send("❌ 收到確認寫入失敗，請稍後再試。", ephemeral=True)
            return
        description = task.description.strip() or "Notion 尚未填寫任務說明。"
        if len(description) > 900:
            description = description[:897].rstrip() + "..."
        content = (
            "✅ 已記錄你收到這項任務，狀態已更新為「進行中」。\n\n"
            f"**任務內容**\n{description}"
        )
        if task.url:
            content += f"\n\n📄 查看完整任務：{task.url}"
        await interaction.followup.send(content, ephemeral=True)

# 顯示順序：藝人要行動的在前，資訊性的在後
SECTION_ORDER = [
    ("overdue", "🔴 已逾期"),
    ("needs_fix", "🛠 需修改"),
    ("due_soon", "⏰ 三天內截止"),
    ("open", "📋 進行中／待提交"),
    ("in_review", "📮 待審核（已提交，等公司確認）"),
]


def _task_line(task: repositories.TaskItem, now: datetime, extra: str = "") -> str:
    type_text = task.task_type or "任務"
    line = f"・**{task.name}**｜{type_text}｜{ui_text.deadline_text(task, now)}"
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


class TaskResendSelect(discord.ui.Select):
    def __init__(self, tasks: list[repositories.TaskItem]):
        self.tasks = {task.page_id: task for task in tasks}
        options = [
            discord.SelectOption(
                label=(task.name or "未命名任務")[:100],
                value=task.page_id,
                description=f"狀態：{task.status or '未設定'}"[:100],
            )
            for task in tasks[:25]
        ]
        super().__init__(placeholder="選擇要重新通知的任務…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        task = self.tasks[self.values[0]]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await repositories.reset_task_discord_notification(task.page_id)
            await audit.log_event(
                event_name=f"重送任務通知｜{task.name}",
                action_type="更新",
                operator=str(interaction.user),
                discord_user_id=str(interaction.user.id),
                target_db="Tasks",
                target_url=task.url,
                after="清除 Discord 通知／收到時間，等待下一輪重新派發",
            )
        except Exception:
            logger.exception("重送任務通知設定失敗：%s", task.page_id)
            await interaction.followup.send("❌ 重送設定失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ 已排入重送：**{task.name}**。Bot 將在下一輪背景檢查（目前每 30 秒）重新通知。",
            ephemeral=True,
        )


class TaskResendView(discord.ui.View):
    def __init__(self, tasks: list[repositories.TaskItem]):
        super().__init__(timeout=180)
        self.add_item(TaskResendSelect(tasks))


class TasksCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="我的任務", description="查看我的未完成、即將到期與待審核任務")
    async def my_tasks(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_task_channel(interaction):
            return
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
            ui_text.trim_message(f"📑 {artist.name} 的任務：\n\n{body}"), ephemeral=True
        )

    @app_commands.command(name="任務總覽", description="管理端：全部未結案任務（待審核／逾期／即將到期）")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def task_overview(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_task_channel(interaction):
            return
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
            ui_text.trim_message(f"📋 未結案任務總覽（{total} 筆）：\n\n{body}"), ephemeral=True
        )

    @app_commands.command(name="任務通知重送", description="管理端：選擇任務重新通知關聯藝人")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def resend_task_notification(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_task_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tasks = [
                task for task in await repositories.get_active_tasks()
                if task.discord_visible
            ]
        except Exception:
            logger.exception("讀取可重送任務失敗")
            await interaction.followup.send("❌ 讀取任務失敗，請稍後再試。", ephemeral=True)
            return
        if not tasks:
            await interaction.followup.send("目前沒有開啟 Discord 顯示的任務。", ephemeral=True)
            return
        await interaction.followup.send(
            "請選擇要重新通知的任務。這只會清除通知紀錄，不會修改任務內容。",
            view=TaskResendView(tasks),
            ephemeral=True,
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
