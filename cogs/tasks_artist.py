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
        # 原始任務通知已經包含完整內容；確認後只回覆狀態，避免重複洗版。
        content = (
            f"✅ 已收到「{task.name}」\n"
            "狀態：進行中\n"
            "請依原任務通知完成；需要查看詳細內容，請按原通知的「查看任務內容」。"
        )
        if task.url:
            content += f"\n📄 {task.url}"
        await interaction.followup.send(content, ephemeral=True)


class TaskConcernButton(discord.ui.DynamicItem[discord.ui.Button], template=r"task_concern:(?P<task_id>[\w-]+)"):
    """任務通知中的持久化問題／無法配合按鈕。"""

    def __init__(self, task_id: str):
        super().__init__(
            discord.ui.Button(
                label="有問題／無法配合",
                style=discord.ButtonStyle.secondary,
                emoji="💬",
                custom_id=f"task_concern:{task_id}",
            )
        )
        self.task_id = task_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("task_id"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TaskConcernModal(self.task_id))


class TaskConcernModal(discord.ui.Modal, title="回報任務問題"):
    def __init__(self, task_id: str):
        super().__init__()
        self.task_id = task_id
        self.reason = discord.ui.TextInput(
            label="請說明原因或需要調整的地方",
            placeholder="例如：時間無法配合、任務內容不清楚、需要改提交方式…",
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        task = await repositories.get_task(self.task_id)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if task is None or artist is None or artist.page_id not in task.artist_page_ids:
            await interaction.followup.send("❌ 這不是分派給你的任務。", ephemeral=True)
            return
        reason = self.reason.value.strip()
        if not reason:
            await interaction.followup.send("❌ 請填寫原因或需要調整的地方。", ephemeral=True)
            return
        now = datetime.now(config.get_tenant().tz)
        entry = (
            f"【Discord 任務回饋｜{now:%Y-%m-%d %H:%M}】\n"
            f"藝人：{artist.name}\n"
            f"回饋：{reason}"
        )
        try:
            # 先保存原因，再將任務退回未發布狀態供控方調整。
            await repositories.append_task_feedback(self.task_id, entry)
            await repositories.return_task_after_artist_concern(self.task_id)
            await audit.log_event(
                event_name=f"藝人回報任務問題｜{task.name}",
                action_type="更新",
                operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Tasks",
                target_url=task.url,
                after=f"退回尚未開始並暫停 Discord 顯示｜{reason}",
            )
        except Exception:
            logger.exception("任務問題回饋寫入失敗：%s", self.task_id)
            await interaction.followup.send("❌ 回饋寫入失敗，請稍後再試。", ephemeral=True)
            return

        admin_channel = (
            interaction.client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
            or interaction.client.get_channel(config.TASK_CHANNEL_ID)
        )
        if admin_channel:
            link = f"\n🔗 [開啟 Notion Task]({task.url})" if task.url else ""
            await admin_channel.send(
                f"⚠️ **藝人回報任務問題**\n"
                f"任務：**{task.name}**\n"
                f"藝人：{artist.name}\n"
                f"回饋：{reason}\n"
                "狀態：已退回「尚未開始」，並取消 Discord 顯示。\n"
                "請修改任務後重新勾選「Discord 顯示」，Bot 會重新通知藝人。"
                f"{link}"
            )
        await interaction.followup.send(
            "✅ 已將問題回報給控方，任務已暫停。控方調整後會重新通知你。",
            ephemeral=True,
        )

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


class MyTasksView(discord.ui.View):
    """藝人任務總覽的操作入口。"""

    def __init__(self, cog: "TasksCog", submittable_count: int):
        super().__init__(timeout=600)
        self.cog = cog
        if submittable_count > 0:
            button = discord.ui.Button(
                label=f"提交任務（{submittable_count}）",
                style=discord.ButtonStyle.primary,
                emoji="📤",
            )
            button.callback = self.open_submissions
            self.add_item(button)

    async def open_submissions(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.open_submissions(interaction)


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
        await interaction.response.defer(ephemeral=True)


class TaskResendView(discord.ui.View):
    def __init__(self, tasks: list[repositories.TaskItem]):
        super().__init__(timeout=180)
        self.select = TaskResendSelect(tasks)
        self.select.callback = self.selection_changed
        self.add_item(self.select)

    async def selection_changed(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="確認重送", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_resend(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.select.values:
            await interaction.response.send_message("請先選擇要重新通知的任務。", ephemeral=True)
            return
        task = self.select.tasks[self.select.values[0]]
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
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="已取消，沒有重送任何任務通知。", view=None)
        self.stop()


class TasksCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _my_tasks_payload(
        self,
        discord_user_id: int,
    ) -> tuple[str, MyTasksView | None]:
        artist = await repositories.find_artist_by_discord_id(discord_user_id)
        if artist is None:
            return (
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                None,
            )
        tasks = await repositories.get_discord_tasks_for_artist(artist.page_id)
        now = datetime.now(config.get_tenant().tz)
        buckets = repositories.classify_tasks_for_display(tasks, now)
        body = format_task_buckets(buckets, now)
        submittable_count = sum(
            1 for task in tasks if repositories.is_task_submittable(task)
        )
        if not body:
            content = "🎉 目前沒有進行中的任務。"
        else:
            content = ui_text.trim_message(f"📑 {artist.name} 的任務：\n\n{body}")
        view = MyTasksView(self, submittable_count) if submittable_count else None
        return content, view

    async def open_submissions(self, interaction: discord.Interaction) -> None:
        submissions_cog = self.bot.get_cog("SubmissionsCog")
        if submissions_cog is None:
            await interaction.edit_original_response(
                content="❌ 提交功能尚未初始化，請稍後再試。",
                view=None,
            )
            return
        try:
            await submissions_cog.edit_submit_task_list(interaction)
        except Exception:
            logger.exception("從我的任務開啟提交流程失敗")
            await interaction.edit_original_response(
                content="❌ 開啟提交流程失敗，請稍後再試。",
                view=None,
            )

    async def return_to_my_tasks(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content, view = await self._my_tasks_payload(interaction.user.id)
        except Exception:
            logger.exception("返回我的任務時讀取失敗")
            content, view = "❌ 讀取任務失敗，請稍後再試。", None
        await interaction.edit_original_response(content=content, view=view)

    @app_commands.command(name="我的任務", description="查看我的未完成、即將到期與待審核任務")
    async def my_tasks(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_task_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            content, view = await self._my_tasks_payload(interaction.user.id)
        except Exception:
            logger.exception("讀取藝人任務失敗")
            await interaction.followup.send("❌ 讀取任務失敗，請稍後再試。", ephemeral=True)
            return
        await interaction.followup.send(content, view=view, ephemeral=True)

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
                if task_notifications.is_resendable_task(task)
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
