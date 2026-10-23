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
from services import audit, notification_log, ops_notice, repositories, task_bundle, task_notifications

logger = logging.getLogger(__name__)


async def clear_old_notifications(
    client, task_ids: set[str], user_id: int | str, keep: tuple[int, int] | None = None,
) -> int:
    """這些任務要發新通知了：同一任務發給同一個人的舊通知先刪掉（M3-27）。

    舊的合併訊息如果還有別的任務沒被取代，就保留不刪（只把這幾筆從它的待處理清單拿掉）。
    `keep` 是不要刪的那則（例如藝人正在按的那則）。回傳刪了幾則。
    """
    state = notification_log.shared()
    seen: set[tuple[int, int]] = set()
    deleted = 0
    for task_id in task_ids:
        kept = None
        for record in notification_log.messages_for(state, task_id, user_id):
            key = (record["channel_id"], record["message_id"])
            if key == keep:
                kept = record
                continue
            if key in seen:
                continue
            seen.add(key)
            bundle_id = record.get("bundle")
            if bundle_id and not task_bundle.supersede(state, bundle_id, task_ids):
                continue  # 舊合併訊息裡還有別的任務要留著
            try:
                await client.get_partial_messageable(key[0]).get_partial_message(key[1]).delete()
                deleted += 1
            except discord.NotFound:
                pass
            except Exception:
                logger.warning("舊任務通知無法刪除：%s／%s", task_id, key[1])
        notification_log.forget_messages(state, task_id, user_id)
        if kept is not None:
            # 留下的那則（例如剛收成一行的）繼續記著：之後重送時才刪得掉，私訊不會留一排舊的收合訊息
            state["messages"].setdefault(str(task_id), {}).setdefault(str(user_id), []).append(kept)
    notification_log.persist()
    return deleted


async def send_task_dm(user: discord.abc.User, task_id: str, content: str, client=None) -> discord.Message:
    """送出帶「收到任務」按鈕的任務通知，並記下訊息編號，之後按收到時才清得掉舊的（M3-23）。

    送之前先刪掉同一任務發給這個人的舊通知（M3-27）：重送時私訊裡只留最新那張。
    """
    if client is not None:
        await clear_old_notifications(client, {task_id}, user.id)
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


def _deadline_fn():
    now = datetime.now(config.get_tenant().tz)
    return lambda task: ui_text.deadline_text(task, now)


def bundle_options(tasks: list[repositories.TaskItem], selected: str | None = None) -> list[discord.SelectOption]:
    deadline = _deadline_fn()
    return [
        discord.SelectOption(
            label=(task.name or "未命名任務")[:100], value=task.page_id,
            description=deadline(task)[:100], default=task.page_id == selected,
        )
        for task in tasks[: task_bundle.MAX_BUNDLE]
    ]


def bundle_view(
    bundle_id: str, options: list[discord.SelectOption], *, received: bool = False,
    reported: set[str] | frozenset = frozenset(),
) -> discord.ui.View | None:
    """合併訊息的元件。收到之後只留「某一筆有問題」。

    回報過問題的那幾筆（M3-28）：不再出現在「某一筆有問題」，收到按鈕改成「其餘 N 筆收到」。
    每一筆都處理完、沒有東西可以按了，回 None。
    """
    open_options = [o for o in options if o.value not in reported]
    if not open_options:
        return None
    view = discord.ui.View(timeout=None)
    if not received:
        view.add_item(BundleDetailSelect(bundle_id, options))
        view.add_item(BundleReceiveButton(bundle_id, task_bundle.receive_label(len(open_options), len(reported))))
    plain = [discord.SelectOption(label=o.label, value=o.value, description=o.description) for o in open_options]
    view.add_item(BundleConcernSelect(bundle_id, plain))
    return view


async def send_bundle_dm(client, user, kind: str, tasks: list[repositories.TaskItem]) -> discord.Message:
    """同一輪 2 筆以上：合併成一則（M3-27）。送之前先刪掉這幾筆的舊通知。"""
    task_ids = [task.page_id for task in tasks]
    await clear_old_notifications(client, set(task_ids), user.id)
    bundle_id = task_bundle.new_bundle_id()
    message = await user.send(
        task_bundle.bundle_text(kind, tasks, _deadline_fn()),
        view=bundle_view(bundle_id, bundle_options(tasks)),
    )
    now = datetime.now(config.get_tenant().tz)
    state = notification_log.shared()
    task_bundle.remember_bundle(state, bundle_id, user.id, kind, task_ids, now)
    for task_id in task_ids:
        notification_log.record_message(state, task_id, user.id, message.channel.id, message.id, now, bundle_id=bundle_id)
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


async def _send_staff_dm(bot, person, tasks: list[repositories.TaskItem], artist_names) -> bool:
    """同仁這一輪的知會：1 筆用原本的同仁版內容，2 筆以上合併成清單。送之前刪掉這幾筆的舊知會。"""
    try:
        user = await bot.fetch_user(int(person.discord_user_id))
        await clear_old_notifications(bot, {task.page_id for task in tasks}, user.id)
        if len(tasks) == 1:
            content = task_notifications.staff_message(task_notifications.notification_message(tasks[0]))
        else:
            content = task_bundle.staff_bundle_text(tasks, artist_names, _deadline_fn())
        message = await user.send(content)
    except Exception:
        logger.exception("相關同仁通知失敗：%s", person.name)
        return False
    now = datetime.now(config.get_tenant().tz)
    state = notification_log.shared()
    for task in tasks:
        notification_log.record_message(state, task.page_id, user.id, message.channel.id, message.id, now)
    notification_log.persist()
    return True


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
    current_key = (current_message.channel.id, current_message.id) if current_message is not None else None
    handled = 0
    if current_message is not None:
        try:
            content = collapsed or (current_message.content + notification_log.received_suffix(received_at))
            await current_message.edit(content=content[:2000], view=_concern_only_view(task_id))
            handled += 1
        except Exception:
            logger.warning("任務通知無法收合：%s／%s", task_id, current_message.id)
    # 舊通知刪掉；但包含這筆的舊合併訊息裡還有別的任務沒處理，就保留（M3-27）
    handled += await clear_old_notifications(client, {task_id}, user_id, keep=current_key)
    return handled


def _concern_only_view(task_id: str) -> discord.ui.View:
    """收到之後仍可回報問題（M3-27）：收成一行的那則留一顆「有問題／無法配合」。"""
    view = discord.ui.View(timeout=None)
    view.add_item(TaskConcernButton(task_id))
    return view


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
        if not task.discord_visible:
            # 回報問題退回控方了（Discord 顯示被取消）：不能再記成收到，不然剛退回的任務又變進行中（M3-28）
            try:
                await interaction.message.edit(
                    content=task_bundle.reported_card_line(task, datetime.now(config.get_tenant().tz)), view=None,
                )
            except Exception:
                logger.warning("已退回任務的卡片無法收合：%s", self.task_id)
            await interaction.followup.send(task_bundle.ALREADY_REPORTED_REPLY, ephemeral=True)
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

    def __init__(self, task_id: str, label: str = "有問題／無法配合"):
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                emoji="⚠️",
                custom_id=f"task_concern:{task_id}",
            )
        )
        self.task_id = task_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("task_id"), item.label or "有問題／無法配合")

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_concern_modal(interaction, self.task_id)


async def open_concern_modal(interaction: discord.Interaction, task_id: str, bundle_id: str | None = None) -> None:
    """已交件、修改中的任務不能用按鈕回報（M3-27）；其他跳出填原因的視窗。只讀欄位，趕在 3 秒內回應。

    記下是從哪則訊息按的（單筆卡、合併通知、早報），送出後那則才能原地收尾（M3-28）。
    """
    try:
        task = await repositories.get_task_brief(task_id)
    except Exception:
        logger.exception("回報問題前讀取任務失敗：%s", task_id)
        task = None
    if task is not None and not task.discord_visible:
        await interaction.response.send_message(task_bundle.ALREADY_REPORTED_REPLY, ephemeral=True)
        return
    if task is not None and not task_bundle.can_report_concern(task):
        await interaction.response.send_message(task_bundle.CONCERN_BLOCKED_REPLY, ephemeral=True)
        return
    origin = getattr(interaction, "message", None)
    await interaction.response.send_modal(TaskConcernModal(task_id, origin=origin, bundle_id=bundle_id))


CONCERN_SENT_REPLY = "✅ 已將問題回報給公司，任務已暫停。公司調整後會重新通知你。"
CONCERN_KEPT_REPLY = "✅ 已將問題回報給公司。這筆已經過了期限，會先維持「已逾期」，公司會跟你確認要延期還是怎麼處理。"


async def after_concern_reported(
    interaction: discord.Interaction, task: repositories.TaskItem, origin, bundle_id: str | None,
) -> None:
    """回報送出後：按的那則原地收尾，剩下沒問題的一鍵收完（M3-28）。

    - 合併通知：劃掉這筆，按鈕變「其餘 N 筆收到」；確認訊息也附同一顆。每一筆都回報了就整則收合。
    - 早報：那一行標「已回報」，確認訊息提醒其他筆也能回報（不附看完按鈕）；每一筆都回報了就直接算已讀。
    - 單筆卡：收成一行「⚠️ 已回報問題」，不留按鈕。
    """
    from cogs import briefing

    now = datetime.now(config.get_tenant().tz)
    view: discord.ui.View | None = None
    reply = CONCERN_KEPT_REPLY if task_bundle.concern_keeps_status(task) else CONCERN_SENT_REPLY
    try:
        if bundle_id:
            pending = await refresh_bundle_after_concern(origin, bundle_id, task.page_id)
            if pending:
                reply += f"\n其他 {pending} 筆沒問題的話，直接按下面就好："
                view = _receive_rest_view(bundle_id, origin, pending)
        elif origin is not None and briefing.is_briefing_message(origin):
            pending = await briefing.mark_reported(interaction, origin, task)
            if pending:
                # 不附「其他我都看到了」：回報完馬上推一顆按鈕，像在催藝人把其他的也認了（2026-10-23 Johnny）
                reply += "\n早報裡其他筆如果也有問題，一樣用「看某一筆的詳細」展開後回報；都看過了再按「👌 我都看到了」。"
        elif origin is not None:
            await origin.edit(content=task_bundle.reported_card_line(task, now), embed=None, view=None)
    except Exception:
        logger.exception("回報問題後收尾訊息失敗：%s", task.page_id)
    if view is not None:
        await interaction.followup.send(reply, view=view, ephemeral=True)
    else:
        await interaction.followup.send(reply, ephemeral=True)


def _receive_rest_view(bundle_id: str, bundle_message, pending: int) -> discord.ui.View:
    """確認訊息上的「其餘 N 筆收到」：只有藝人看得到，按了等於在合併通知上按收到。"""
    view = discord.ui.View(timeout=900)
    button = discord.ui.Button(label=f"其餘 {pending} 筆收到", emoji="✅", style=discord.ButtonStyle.success)

    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        count = await receive_bundle(interaction, bundle_id, bundle_message)
        if count:
            await interaction.edit_original_response(content=f"✅ 已收到其餘 {count} 筆。", view=None)

    button.callback = callback
    view.add_item(button)
    return view


class TaskConcernModal(discord.ui.Modal, title="回報任務問題"):
    def __init__(self, task_id: str, *, origin=None, bundle_id: str | None = None):
        super().__init__()
        self.task_id = task_id
        self.origin = origin
        self.bundle_id = bundle_id
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
        if not task_bundle.can_report_concern(task):
            await interaction.followup.send(task_bundle.CONCERN_BLOCKED_REPLY, ephemeral=True)
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
        keep = task_bundle.concern_keeps_status(task)
        try:
            # 先保存原因，再將任務退回未發布狀態供控方調整。
            # 已逾期的不退回（2026-10-23）：保留逾期紀錄，控方決定延期或結案
            await repositories.append_task_feedback(self.task_id, entry)
            if not keep:
                await repositories.return_task_after_artist_concern(self.task_id)
            await audit.log_event(
                event_name=f"藝人回報任務問題｜{task.name}",
                action_type="更新",
                operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Tasks",
                target_url=task.url,
                after=(f"維持{task.status}，未退回｜{reason}" if keep
                       else f"退回尚未開始並暫停 Discord 顯示｜{reason}"),
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
            + (
                f"狀態：維持「{task.status}」，沒有退回（保留逾期紀錄）。\n"
                "請決定要延期（改截止日或提交期限）還是結案。"
                if keep else
                "狀態：已退回「尚未開始」，並取消 Discord 顯示。\n"
                "請修改任務後重新勾選「Discord 顯示」，Bot 會重新通知藝人。"
            )
            + link,
            view=ops_action_view(),
        )
        await after_concern_reported(interaction, task, self.origin, self.bundle_id)


def _bundle_ids_from_message(message) -> list[str]:
    """合併訊息裡有哪幾筆：狀態檔被清掉時從訊息上的選單讀，照樣按得動。"""
    ids: list[str] = []
    for row in getattr(message, "components", []) or []:
        for child in getattr(row, "children", []) or []:
            if str(getattr(child, "custom_id", "")).startswith(("bundle_detail:", "bundle_concern:")):
                ids += [o.value for o in getattr(child, "options", []) if o.value not in ids]
    return ids


class BundleDetailSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"bundle_detail:(?P<bundle_id>[0-9a-f]{12})",
):
    """合併訊息的「看某一筆的詳細」：展開在同一則（embed），再選別筆就換成那一筆（M3-27）。"""

    def __init__(self, bundle_id: str, options: list[discord.SelectOption] | None = None):
        self.options = options or [discord.SelectOption(label="（沒有任務）", value="none")]
        super().__init__(discord.ui.Select(
            placeholder="看某一筆的詳細…", options=self.options,
            custom_id=f"bundle_detail:{bundle_id}", min_values=1, max_values=1,
        ))
        self.bundle_id = bundle_id

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match):
        return cls(match.group("bundle_id"), list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        values = (interaction.data or {}).get("values") or []
        task_id = values[0] if values else ""
        task = await repositories.get_task(task_id) if task_id and task_id != "none" else None
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if task is None or artist is None or artist.page_id not in task.artist_page_ids:
            await interaction.followup.send("❌ 找不到這筆任務，可能已經結案。用 `/我的任務` 看最新清單。", ephemeral=True)
            return
        entry = task_bundle.bundle_for(notification_log.shared(), self.bundle_id) or {}
        if entry.get("kind") == task_bundle.KIND_ASSIGNMENT:
            text = task_notifications.assignment_notification_message(task)
        else:
            text = task_notifications.notification_message(task)
        text = task_notifications.strip_artist_prompts(text)
        embed = discord.Embed(description=text[:4000], color=0x5865F2)
        embed.set_footer(text="再選別筆會換成那一筆；看完按「全部收到」")
        options = [
            discord.SelectOption(label=o.label, value=o.value, description=o.description, default=o.value == task_id)
            for o in self.options
        ]
        reported = set(entry.get("reported") or [])
        await interaction.edit_original_response(embed=embed, view=bundle_view(self.bundle_id, options, reported=reported))


class BundleReceiveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"bundle_receive:(?P<bundle_id>[0-9a-f]{12})",
):
    """合併訊息的「✅ 全部收到」：每一筆都記成收到任務，整則收成一行，留「某一筆有問題」（M3-27）。"""

    def __init__(self, bundle_id: str, label: str = "全部收到"):
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"bundle_receive:{bundle_id}",
        ))
        self.bundle_id = bundle_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("bundle_id"), item.label or "全部收到")

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await receive_bundle(interaction, self.bundle_id, interaction.message)


async def receive_bundle(interaction: discord.Interaction, bundle_id: str, message) -> int:
    """合併訊息裡還沒回報問題的每一筆都記成收到，整則收成一行（M3-27）。

    合併訊息上的「全部收到／其餘 N 筆收到」和回報後確認訊息上的「其餘 N 筆收到」（M3-28）共用這裡，
    `message` 一律是合併訊息本身。回傳這次收到幾筆。
    """
    artist = await repositories.find_artist_by_discord_id(interaction.user.id)
    if artist is None:
        await interaction.followup.send("❌ 找不到你的藝人資料，請聯絡公司。", ephemeral=True)
        return 0
    now = datetime.now(config.get_tenant().tz)
    state = notification_log.shared()
    entry = task_bundle.bundle_for(state, bundle_id)
    reported_ids = set((entry or {}).get("reported") or [])
    here = (message.channel.id, message.id) if message is not None else None
    task_ids = list(entry["task_ids"]) if entry and entry.get("task_ids") else _bundle_ids_from_message(message)
    mine: list[repositories.TaskItem] = []
    reported: list[repositories.TaskItem] = []
    for task_id in task_ids:
        try:
            task = await repositories.get_task_brief(task_id)
        except Exception:
            logger.exception("全部收到：讀取任務失敗：%s", task_id)
            continue
        if task is None or artist.page_id not in task.artist_page_ids:
            continue
        if task_id in reported_ids or not task.discord_visible:
            reported.append(task)  # 這筆已經回報問題退回控方（Discord 顯示被取消），不能再記成收到
            continue
        if task_notifications.can_acknowledge(task):
            try:
                stored = await repositories.acknowledge_task(task_id, now)
                await audit.log_event(
                    event_name=f"藝人收到任務｜{task.name}", action_type="更新", operator=artist.name,
                    discord_user_id=str(interaction.user.id), target_db="Tasks", target_url=task.url,
                    after=f"狀態：進行中｜收到時間：{now.isoformat()}｜合併通知全部收到"
                    + ("" if stored else "｜收到時間欄位尚未建立"),
                )
            except Exception:
                logger.exception("全部收到：寫入失敗：%s", task_id)
                await interaction.followup.send("❌ 收到確認寫入失敗，請稍後再試。", ephemeral=True)
                return 0
        mine.append(task)
        await clear_old_notifications(interaction.client, {task_id}, interaction.user.id, keep=here)
    if not mine:
        await interaction.followup.send(
            task_bundle.ALREADY_REPORTED_REPLY if reported else "❌ 這不是分派給你的任務。", ephemeral=True,
        )
        return 0
    if entry is not None:
        entry["received"] = [task.page_id for task in mine]
        entry["reported"] = [task.page_id for task in reported]
        notification_log.persist()
    try:
        await message.edit(
            content=task_bundle.done_text(mine, reported, now, _deadline_fn()), embed=None,
            view=bundle_view(bundle_id, bundle_options(mine), received=True),
        )
    except Exception:
        logger.warning("合併通知無法收合：%s", bundle_id)
    logger.info("全部收到：%s 筆（%s）", len(mine), artist.name)
    return len(mine)


async def refresh_bundle_after_concern(message, bundle_id: str, task_id: str) -> int:
    """合併訊息裡有一筆回報了問題：原地重畫，劃掉那筆（M3-28）。回傳還有幾筆等著按收到。

    狀態檔沒有這則合併訊息（例如被清掉）就不動訊息，回 0。
    """
    state = notification_log.shared()
    entry = task_bundle.mark_reported(state, bundle_id, task_id)
    notification_log.persist()
    if entry is None or message is None:
        return 0
    tasks: list[repositories.TaskItem] = []
    for tid in entry.get("task_ids") or []:
        try:
            task = await repositories.get_task_brief(tid)
        except Exception:
            logger.exception("回報後重畫合併通知：讀取任務失敗：%s", tid)
            task = None
        if task is not None:
            tasks.append(task)
    reported_ids = set(entry.get("reported") or [])
    reported = [task for task in tasks if task.page_id in reported_ids]
    now = datetime.now(config.get_tenant().tz)
    received_ids = entry.get("received")
    if received_ids is not None:
        # 已經按過收到：只剩「某一筆有問題」，把這筆從收到清單移到已回報
        received = [task for task in tasks if task.page_id in received_ids and task.page_id not in reported_ids]
        await message.edit(
            content=task_bundle.done_text(received, reported, now, _deadline_fn()), embed=None,
            view=bundle_view(bundle_id, bundle_options(received), received=True) if received else None,
        )
        return 0
    pending = [task for task in tasks if task.page_id not in reported_ids]
    if not pending:
        await message.edit(content=task_bundle.done_text([], reported, now, _deadline_fn()), embed=None, view=None)
        return 0
    await message.edit(
        content=task_bundle.bundle_text(entry.get("kind", task_bundle.KIND_DISPATCH), tasks, _deadline_fn(), reported_ids),
        embed=None,
        view=bundle_view(bundle_id, bundle_options(tasks), reported=reported_ids),
    )
    return len(pending)


class BundleConcernSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"bundle_concern:(?P<bundle_id>[0-9a-f]{12})",
):
    """合併訊息的「⚠️ 某一筆有問題」：選一筆、填原因，只退回那一筆（M3-27）。收到前後都在。"""

    def __init__(self, bundle_id: str, options: list[discord.SelectOption] | None = None):
        self.options = options or [discord.SelectOption(label="（沒有任務）", value="none")]
        super().__init__(discord.ui.Select(
            placeholder="⚠️ 某一筆有問題…", options=self.options,
            custom_id=f"bundle_concern:{bundle_id}", min_values=1, max_values=1,
        ))
        self.bundle_id = bundle_id

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match):
        return cls(match.group("bundle_id"), list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or values[0] == "none":
            await interaction.response.defer()
            return
        await open_concern_modal(interaction, values[0], bundle_id=self.bundle_id)

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
    """單筆任務的派發通知（/任務通知重送 等）。回傳 None 代表沒有任何有效收件人。"""
    return (await send_task_notifications(bot, [task]))[task.page_id]


async def send_task_notifications(
    bot, tasks: list[repositories.TaskItem], kind: str = task_bundle.KIND_DISPATCH,
) -> dict[str, DispatchOutcome | None]:
    """這一輪要通知的任務，依收件人分組送出（M3-27）。

    同一位藝人 1 筆 → 原本的任務卡；2 筆以上 → 一則合併訊息（最多 25 筆一則）。
    相關同仁同樣分組（課後作業通知不發同仁，跟原本一樣）。送之前先刪掉同一任務發給同一個人的舊通知。
    回傳每筆任務的結果；None 代表那筆沒有任何有效收件人（藝人與同仁都沒有，或都缺 Discord User ID）。
    派發（kind=dispatch）全部送達的任務會發公開公告、寫「Discord 通知時間」；課後作業由呼叫端記錄。
    """
    artist_ids = sorted({pid for task in tasks for pid in task.artist_page_ids})
    artists = {a.page_id: a for a in await repositories.get_artists_by_ids(artist_ids)} if artist_ids else {}
    staff_ids = sorted({pid for task in tasks for pid in task.staff_page_ids}) if kind == task_bundle.KIND_DISPATCH else []
    people = {p.page_id: p for p in await repositories.get_people_items_by_ids(staff_ids)} if staff_ids else {}

    outcomes: dict[str, DispatchOutcome | None] = {}
    per_artist: dict[str, list[repositories.TaskItem]] = {}
    per_staff: dict[str, list[repositories.TaskItem]] = {}
    for task in tasks:
        notifyable_artists = [artists[pid] for pid in task.artist_page_ids if pid in artists and artists[pid].discord_user_id]
        notifyable_staff = [people[pid] for pid in task.staff_page_ids if pid in people and people[pid].discord_user_id]
        if not notifyable_artists and not notifyable_staff:
            outcomes[task.page_id] = None
            continue
        outcomes[task.page_id] = DispatchOutcome(notifyable_artists=notifyable_artists, notifyable_staff=notifyable_staff)
        for artist in notifyable_artists:
            per_artist.setdefault(artist.page_id, []).append(task)
        for person in notifyable_staff:
            per_staff.setdefault(person.page_id, []).append(task)

    for artist_id, artist_tasks in per_artist.items():
        artist = artists[artist_id]
        try:
            user = await bot.fetch_user(int(artist.discord_user_id))
        except Exception:
            logger.exception("任務通知失敗（找不到 Discord 使用者）：%s", artist.name)
            continue
        for group in task_bundle.chunks(artist_tasks):
            try:
                if len(group) == 1:
                    task = group[0]
                    # 任務說明已經寫在訊息裡，藝人不需要開 Notion。
                    content = (
                        task_notifications.assignment_notification_message(task)
                        if kind == task_bundle.KIND_ASSIGNMENT else task_notifications.notification_message(task)
                    )
                    await send_task_dm(user, task.page_id, content, client=bot)
                else:
                    await send_bundle_dm(bot, user, kind, group)
            except Exception:
                logger.exception("任務通知失敗：%s／%s", artist.name, "、".join(t.name for t in group))
                continue
            for task in group:
                outcomes[task.page_id].sent += 1

    def artist_names(task):
        return [artists[pid].name for pid in task.artist_page_ids if pid in artists]

    for person_id, person_tasks in per_staff.items():
        for group in task_bundle.chunks(person_tasks):
            if await _send_staff_dm(bot, people[person_id], group, artist_names):
                for task in group:
                    outcomes[task.page_id].staff_sent += 1

    for task in tasks:
        outcome = outcomes.get(task.page_id)
        if outcome is None or not outcome.is_complete or kind != task_bundle.KIND_DISPATCH:
            continue
        await _after_dispatch(bot, task, outcome)
    return outcomes


async def refresh_unreceived_cards(bot, user, tasks: list[repositories.TaskItem]) -> int:
    """還沒按收到的任務卡搬到最下面（M3-29）：舊卡刪掉、重新送一張，2 筆以上合併。

    只送藝人本人：不發同仁、不發公告、不改 Notion（這不是重新派發，只是把卡移到看得到的位置）。
    回傳送出幾則。
    """
    sent = 0
    for group in task_bundle.chunks(tasks):
        try:
            if len(group) == 1:
                await send_task_dm(user, group[0].page_id, task_notifications.notification_message(group[0]), client=bot)
            else:
                await send_bundle_dm(bot, user, task_bundle.KIND_DISPATCH, group)
            sent += 1
        except Exception:
            logger.exception("搬移還沒按收到的任務卡失敗：%s", "、".join(t.name for t in group))
    return sent


async def _after_dispatch(bot, task: repositories.TaskItem, outcome: DispatchOutcome) -> None:
    """派發全部送達後：公開類型發公告區、寫「Discord 通知時間」。"""
    logger.info("任務派發通知成功：%s，已通知 %s 位藝人、%s 位同仁", task.name, outcome.sent, outcome.staff_sent)
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
