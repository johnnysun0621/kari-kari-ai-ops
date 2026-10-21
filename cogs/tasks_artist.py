"""P0-2 任務查詢：藝人 /我的任務、管理端 /任務總覽。"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard, paging, ui_text
from services.permissions import is_admin
from services import audit, notification_log, ops_notice, repositories, task_notifications

logger = logging.getLogger(__name__)


async def send_task_dm(user: discord.abc.User, task_id: str, content: str) -> discord.Message:
    """送出帶「收到任務」按鈕的任務通知，並記下訊息編號，之後按收到時才清得掉舊的（M3-23）。"""
    view = discord.ui.View(timeout=None)
    view.add_item(TaskReceiptButton(task_id))
    view.add_item(TaskConcernButton(task_id))
    # 不掛 Notion 連結按鈕：任務頁往上翻就是整個 Tasks，藝人會看到其他人的任務。
    message = await user.send(content, view=view)
    state = notification_log.shared()
    notification_log.record_message(
        state, task_id, user.id, message.channel.id, message.id,
        datetime.now(config.get_tenant().tz),
    )
    notification_log.persist()
    return message


async def notify_staff(
    bot,
    task: repositories.TaskItem,
    artist_content: str,
    staff: list[repositories.PersonItem] | None = None,
) -> int:
    """私訊任務的相關同仁（M6-05）。沒有按鈕：收到／延期按鈕只認藝人。

    沒有 Discord ID 的同仁略過，單人失敗不影響其他人；回傳實際送出幾則。
    """
    if staff is None:
        staff = await repositories.get_people_items_by_ids(task.staff_page_ids)
    content = task_notifications.staff_message(artist_content)
    sent = 0
    for person in staff:
        if not person.discord_user_id:
            continue
        try:
            user = await bot.fetch_user(int(person.discord_user_id))
            await user.send(content)
            sent += 1
        except Exception:
            logger.exception("相關同仁通知失敗：%s／%s", task.name, person.name)
    return sent


def received_line(task: repositories.TaskItem, received_at: datetime) -> str:
    """派發卡按了「收到任務」後收成的那一行。"""
    now = datetime.now(config.get_tenant().tz)
    return f"✅ 已收到｜{task.name}（{ui_text.deadline_text(task, now)}）｜{received_at:%m/%d %H:%M}"


async def mark_previous_notifications_received(
    client: discord.Client,
    task_id: str,
    user_id: int,
    received_at: datetime,
    current_message: discord.Message | None = None,
    collapsed: str | None = None,
) -> int:
    """按了「收到任務」：按的那則收成一行，同一任務發給這位藝人的其他舊通知刪掉。

    2026-10-20（M3-24）：原本每則舊通知各加一行「✅ 已收到」，私訊裡會疊成一排，改成只留一行。
    只動這位藝人自己的訊息，不影響其他任務、其他藝人。訊息被刪或拿不到時略過，回傳處理了幾則。
    """
    state = notification_log.shared()
    targets = {(m["channel_id"], m["message_id"]) for m in notification_log.messages_for(state, task_id, user_id)}
    current_key = (current_message.channel.id, current_message.id) if current_message is not None else None
    if current_key:
        targets.discard(current_key)
    handled = 0
    if current_message is not None:
        try:
            content = collapsed or (current_message.content + notification_log.received_suffix(received_at))
            await current_message.edit(content=content[:2000], view=None)
            handled += 1
        except Exception:
            logger.warning("任務通知無法收合：%s／%s", task_id, current_message.id)
    for channel_id, message_id in targets:
        try:
            await client.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
            handled += 1
        except discord.NotFound:
            pass
        except Exception:
            logger.warning("舊任務通知無法刪除：%s／%s", task_id, message_id)
    notification_log.forget_messages(state, task_id, user_id)
    notification_log.persist()
    return handled


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
        # 先回應 Discord（不另發訊息，只更新這則），再進行 Notion 查詢，避免超過 3 秒互動期限。
        await interaction.response.defer()
        task = await repositories.get_task(self.task_id)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if task is None or artist is None or artist.page_id not in task.artist_page_ids:
            await interaction.followup.send("❌ 這不是分派給你的任務。", ephemeral=True)
            return
        if not task_notifications.can_acknowledge(task):
            # 已經收到過（或已進入後續狀態）：直接收合，不再多跳一則說明。
            received = task.discord_acknowledged_at or datetime.now(config.get_tenant().tz)
            await mark_previous_notifications_received(
                interaction.client, self.task_id, interaction.user.id, received,
                interaction.message, received_line(task, received),
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
        handled = await mark_previous_notifications_received(
            interaction.client, self.task_id, interaction.user.id, received_at,
            interaction.message, received_line(task, received_at),
        )
        # 不再另外回一則「已收到」：按的那則自己收成一行就是回饋，私訊不會越疊越長（M3-24）。
        logger.info("收到任務：%s，收合／清掉 %s 則通知", task.name, handled)


class ReminderAckButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"reminder_ack:(?P<task_id>[\w-]+):(?P<stage_code>d\d)",
):
    """截止前提醒（T-5／當天）的持久化「收到通知」按鈕。

    跟「收到任務」不同：這個不改任務狀態，只在 Notion「提醒確認」追加一行
    「階段｜時間｜藝人」，讓控方分得出誰看到了提醒、誰沒反應。逾期通知沒有這顆。
    """

    def __init__(self, task_id: str, stage_code: str):
        super().__init__(
            discord.ui.Button(
                label="收到通知",
                style=discord.ButtonStyle.success,
                emoji="👌",
                custom_id=f"reminder_ack:{task_id}:{stage_code}",
            )
        )
        self.task_id = task_id
        self.stage_code = stage_code

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match,
    ) -> "ReminderAckButton":
        return cls(match.group("task_id"), match.group("stage_code"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()  # 只更新這則，不另發訊息（M3-24）
        stage = task_notifications.reminder_stage_from_code(self.stage_code)
        task = await repositories.get_task(self.task_id)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if (
            stage is None
            or task is None
            or artist is None
            or artist.page_id not in task.artist_page_ids
        ):
            await interaction.followup.send("❌ 這不是分派給你的任務。", ephemeral=True)
            return
        now = datetime.now(config.TZ)
        line = task_notifications.format_reminder_ack_line(stage, now, artist.name)
        try:
            stored = await repositories.append_reminder_ack_line(task.page_id, line)
            await audit.log_event(
                event_name=f"藝人確認提醒｜{task.name}",
                action_type="更新",
                operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Tasks",
                target_url=task.url,
                after=line + ("" if stored else "｜Notion 尚無提醒確認欄位，只留此紀錄"),
            )
        except Exception:
            logger.exception("提醒確認寫入失敗：%s", self.task_id)
            await interaction.followup.send("❌ 確認寫入失敗，請稍後再試。", ephemeral=True)
            return
        try:
            await interaction.message.edit(
                content=f"👌 已收到｜{task.name}（{stage}提醒）｜{now:%m/%d %H:%M}", view=None,
            )
        except Exception:
            logger.warning("提醒訊息無法收合：%s", self.task_id)


def reminder_view(task_id: str, stage: str) -> discord.ui.View | None:
    """截止前提醒用的 View；逾期階段沒有確認按鈕，回 None。"""
    code = task_notifications.reminder_stage_code(stage)
    if code is None:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(ReminderAckButton(task_id, code))
    return view


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
            placeholder="例如：時間無法配合、任務內容不清楚、需要延期…",
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

        from cogs.ops_actions import ops_action_view

        link = f"\n🔗 {task.url}" if task.url else ""
        await channel_guard.try_send(
            channel_guard.ops_channel(interaction.client),
            content=ops_notice.action_title(f"藝人回報任務問題：{task.name}") + "\n"
            f"藝人：{artist.name}\n"
            f"回饋：{reason}\n"
            "狀態：已退回「尚未開始」，並取消 Discord 顯示。\n"
            "請修改任務後重新勾選「Discord 顯示」，Bot 會重新通知藝人。"
            f"{link}",
            view=ops_action_view(),
        )
        await interaction.followup.send(
            "✅ 已將問題回報給控方，任務已暫停。控方調整後會重新通知你。",
            ephemeral=True,
        )

# 顯示順序：藝人要行動的在前，資訊性的在後
SECTION_ORDER = [
    ("overdue", "🔴 已逾期"),
    ("needs_fix", "🛠 需修改"),
    ("awaiting", "⏳ 已過截止，等公司確認"),
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


def task_bucket_sections(
    buckets: dict[str, list[repositories.TaskItem]],
    now: datetime,
    name_of_artist: dict[str, str] | None = None,
) -> list[tuple[str, list[str]]]:
    """把分區結果整理成 (分區標題, 任務行) ；供直接輸出或分頁使用。"""
    sections: list[tuple[str, list[str]]] = []
    for key, title in SECTION_ORDER:
        tasks = buckets.get(key, [])
        if not tasks:
            continue
        task_lines: list[str] = []
        for task in tasks:
            extra = ""
            names: list[str] = []
            if name_of_artist is not None:
                names = [name_of_artist.get(pid, "") for pid in task.artist_page_ids]
                names = [n for n in names if n]
                extra = "、".join(names) or "（未關聯藝人）"
            task_lines.append(_task_line(task, now, extra))
            # 控方端才顯示提醒確認：誰按了、誰沒反應，不用開 Notion。藝人端不顯示。
            if name_of_artist is not None:
                ack = task_notifications.reminder_ack_summary(task, names)
                if ack:
                    task_lines.append(f"　　{ack}")
        sections.append((f"**{title}**（{len(tasks)}）", task_lines))
    return sections


def format_task_buckets(
    buckets: dict[str, list[repositories.TaskItem]],
    now: datetime,
    name_of_artist: dict[str, str] | None = None,
) -> str:
    """把分區結果排成 Discord 訊息；name_of_artist 給管理端顯示藝人名。"""
    lines: list[str] = []
    for title, task_lines in task_bucket_sections(buckets, now, name_of_artist):
        lines.append(title)
        lines.extend(task_lines)
        lines.append("")
    return "\n".join(lines).strip()


def task_list_pages(
    header: str,
    buckets: dict[str, list[repositories.TaskItem]],
    now: datetime,
    name_of_artist: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """把任務清單切成多頁訊息，讓任務多的時候可以翻頁看完，而不是被截斷。

    回傳 (每頁文字, 每頁的分區標籤)；標籤給跳頁下拉用，讓人知道哪一頁有逾期、
    哪一頁是待審核。
    """
    sections = task_bucket_sections(buckets, now, name_of_artist)
    # 60 字保留給頁碼與標題後的空行。
    budget = ui_text.DISCORD_MESSAGE_LIMIT - len(header) - 60
    line_pages = ui_text.paginate_sections(sections, budget)
    total = len(line_pages)
    pages: list[str] = []
    labels: list[str] = []
    for index, lines in enumerate(line_pages):
        page_header = header if total == 1 else f"{header}（第 {index + 1}/{total} 頁）"
        body = "\n".join(lines).strip()
        pages.append(ui_text.trim_message(f"{page_header}\n\n{body}"))
        # 這頁涵蓋哪些分區；「（續）」去掉才不會兩頁看起來一樣。
        titles = [
            line.strip().strip("*").split("（")[0]
            for line in lines
            if line.strip().startswith("**")
        ]
        labels.append("、".join(dict.fromkeys(titles)))
    return pages, labels


class MyTasksView(paging.PagedTextView):
    """藝人任務總覽的操作入口；任務多時可翻頁看完全部。"""

    def __init__(
        self,
        cog: "TasksCog",
        submittable_count: int,
        *,
        pages: list[str] | None = None,
        operator_id: int = 0,
        page_labels: list[str] | None = None,
    ):
        super().__init__(pages or [""], operator_id, timeout=600, page_labels=page_labels)
        self.cog = cog
        if submittable_count > 0:
            button = discord.ui.Button(
                label=f"提交任務（{submittable_count}）",
                style=discord.ButtonStyle.primary,
                emoji="📤",
                row=0,
            )
            button.callback = self.open_submissions
            self.add_item(button)

    async def open_submissions(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.open_submissions(interaction)


@dataclass
class DispatchOutcome:
    """單筆任務派發通知的結果；供背景 loop 與 /任務通知重送 共用同一套發送邏輯。"""
    notifyable_artists: list[repositories.Artist] = field(default_factory=list)
    sent: int = 0
    notifyable_staff: list[repositories.PersonItem] = field(default_factory=list)
    staff_sent: int = 0

    @property
    def is_complete(self) -> bool:
        # 有藝人時只看藝人：同仁發失敗就重送整筆，藝人會重複收到。只掛同仁的任務才看同仁。
        if self.notifyable_artists:
            return self.sent == len(self.notifyable_artists)
        return bool(self.notifyable_staff) and self.staff_sent == len(self.notifyable_staff)


async def send_task_notification(bot, task: repositories.TaskItem) -> DispatchOutcome | None:
    """對任務關聯的藝人與相關同仁送出派發通知（私訊＋公開類型另發公告頻道）。

    回傳 None 代表沒有找到任何有效收件人（藝人與同仁都沒有，或都缺 Discord User ID）；
    呼叫端各自決定要怎麼提示（背景 loop 發警告到管理頻道，重送指令直接回覆操作者）。
    """
    artists = await repositories.get_artists_by_ids(task.artist_page_ids)
    notifyable_artists = [artist for artist in artists if artist.discord_user_id]
    staff = await repositories.get_people_items_by_ids(task.staff_page_ids)
    notifyable_staff = [person for person in staff if person.discord_user_id]
    if not notifyable_artists and not notifyable_staff:
        return None
    # 任務說明已經寫在 notification_message 裡，藝人不需要開 Notion。
    content = task_notifications.notification_message(task)
    sent = 0
    for artist in notifyable_artists:
        try:
            user = await bot.fetch_user(int(artist.discord_user_id))
            await send_task_dm(user, task.page_id, content)
            sent += 1
        except Exception:
            logger.exception("任務派發通知失敗：%s／%s", task.name, artist.name)
    staff_sent = await notify_staff(bot, task, content, notifyable_staff) if notifyable_staff else 0
    outcome = DispatchOutcome(
        notifyable_artists=notifyable_artists, sent=sent,
        notifyable_staff=notifyable_staff, staff_sent=staff_sent,
    )
    if outcome.is_complete:
        logger.info("任務派發通知成功：%s，已通知 %s 位藝人、%s 位同仁", task.name, sent, staff_sent)
        if task_notifications.is_public_announcement_task(
            task, config.get_tenant().public_task_types
        ):
            announce_channel = bot.get_channel(config.get_tenant().resolved_announce_channel_id)
            if announce_channel is not None:
                try:
                    await announce_channel.send(task_notifications.public_announcement_message(task))
                    await audit.log_event(
                        event_name=f"任務公開通知｜{task.name}",
                        action_type="建立",
                        operator="System",
                        target_db="Tasks",
                        target_url=task.url,
                        after=f"公告頻道 {announce_channel.id}",
                    )
                except Exception:
                    logger.exception("任務公開公告發送失敗：%s", task.name)
        try:
            await repositories.mark_task_discord_notified(task.page_id)
        except Exception:
            logger.exception("任務通知時間寫入 Notion 失敗：%s", task.page_id)
    return outcome


class TaskResendSelect(discord.ui.Select):
    def __init__(self, tasks: list[repositories.TaskItem]):
        self.tasks = {task.page_id: task for task in tasks}
        options = [
            discord.SelectOption(
                label=(task.name or "未命名任務")[:100],
                value=task.page_id,
                # 標出藝人確認狀態：已確認的重送會要求對方重新按一次。
                description=(
                    f"{'✅ 已確認' if task.discord_acknowledged_at else '⏳ 未確認'}"
                    f"｜狀態：{task.status or '未設定'}"
                )[:100],
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
            # 清掉舊的通知／收到時間，讓這次重送視為全新一輪，藝人需要重新按「收到任務」。
            await repositories.reset_task_discord_notification(task.page_id)
        except Exception:
            logger.exception("重送任務通知設定失敗：%s", task.page_id)
            await interaction.followup.send("❌ 重送設定失敗，請稍後再試。", ephemeral=True)
            return
        # 重送是操作者當下主動要求的動作，直接送出，不等下一輪背景檢查。
        outcome = await send_task_notification(interaction.client, task)
        if outcome is None:
            await interaction.followup.send(
                f"❌ **{task.name}** 找不到有效的關聯藝人或 Discord User ID，請先回 Notion 補齊後再重送。",
                ephemeral=True,
            )
            return
        await audit.log_event(
            event_name=f"重送任務通知｜{task.name}",
            action_type="更新",
            operator=str(interaction.user),
            discord_user_id=str(interaction.user.id),
            target_db="Tasks",
            target_url=task.url,
            after=f"立即重新通知 {outcome.sent}／{len(outcome.notifyable_artists)} 位藝人",
        )
        if outcome.is_complete:
            await interaction.followup.send(
                f"✅ 已重新通知：**{task.name}**（{outcome.sent} 位藝人）。",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ **{task.name}** 部分通知失敗（成功 {outcome.sent}／應通知 "
                f"{len(outcome.notifyable_artists)}），請稍後再試一次 `/任務通知重送`。",
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
        submittable_count = sum(
            1 for task in tasks if repositories.is_task_submittable(task)
        )
        if not any(buckets.get(key) for key, _ in SECTION_ORDER):
            return "🎉 目前沒有進行中的任務。", None
        pages, page_labels = task_list_pages(f"📑 {artist.name} 的任務：", buckets, now)
        if submittable_count == 0 and len(pages) == 1:
            return pages[0], None
        view = MyTasksView(
            self, submittable_count, pages=pages, operator_id=discord_user_id,
            page_labels=page_labels,
        )
        return view.current_text(), view

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
        await interaction.followup.send(content, view=view or discord.utils.MISSING, ephemeral=True)

    @app_commands.command(name="任務總覽", description="管理端：全部未結案任務（待審核／逾期／即將到期）")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def task_overview(self, interaction: discord.Interaction):
        if not await channel_guard.ensure_admin_channel(interaction):
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
        if not any(buckets.get(key) for key, _ in SECTION_ORDER):
            await interaction.followup.send("目前沒有未結案的任務。", ephemeral=True)
            return
        total = sum(len(v) for v in buckets.values())
        pages, page_labels = task_list_pages(
            f"📋 未結案任務總覽（{total} 筆）：", buckets, now, name_of_artist=name_of,
        )
        # 任務多到一頁放不下時給翻頁與跳頁，讓控方能直接跳到逾期那一頁。
        view = (
            paging.PagedTextView(pages, interaction.user.id, page_labels=page_labels)
            if len(pages) > 1
            else discord.utils.MISSING
        )
        await interaction.followup.send(pages[0], view=view, ephemeral=True)

    @app_commands.command(name="任務通知重送", description="管理端：選擇任務重新通知關聯藝人")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    @app_commands.describe(關鍵字="用任務名稱片段縮小範圍；任務多的時候用這個比翻頁快")
    async def resend_task_notification(
        self,
        interaction: discord.Interaction,
        關鍵字: str | None = None,
    ):
        if not await channel_guard.ensure_admin_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            resendable = [
                task for task in await repositories.get_active_tasks()
                if task_notifications.is_resendable_task(task)
            ]
        except Exception:
            logger.exception("讀取可重送任務失敗")
            await interaction.followup.send("❌ 讀取任務失敗，請稍後再試。", ephemeral=True)
            return
        if not resendable:
            await interaction.followup.send("目前沒有開啟 Discord 顯示的任務。", ephemeral=True)
            return

        matched = task_notifications.filter_tasks_by_keyword(resendable, 關鍵字)
        if not matched:
            await interaction.followup.send(
                f"找不到名稱含「{關鍵字}」的任務。目前可重送的共 {len(resendable)} 筆，"
                "換個關鍵字或留空重試。",
                ephemeral=True,
            )
            return
        tasks = task_notifications.sort_resendable_tasks(matched)

        header = "請選擇要重新通知的任務。這只會清除通知紀錄，不會修改任務內容。\n" \
                 "已按過「收到任務」的任務也可以重送（標示 ✅），重送後藝人需要重新確認一次。"
        if 關鍵字:
            header += f"\n🔍 關鍵字「{關鍵字}」：符合 {len(tasks)} 筆（共 {len(resendable)} 筆可重送）。"
        # Discord 下拉硬上限 25；未確認的已排前面，被截掉的要講出來而不是默默消失。
        hidden = max(0, len(tasks) - 25)
        if hidden:
            header += (
                f"\n⚠️ 僅列出前 25 筆（另有 {hidden} 筆未列出）。"
                "請用 `關鍵字` 參數縮小範圍，例如 `/任務通知重送 關鍵字:編曲`。"
            )
        await interaction.followup.send(header, view=TaskResendView(tasks), ephemeral=True)

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
