"""每日早報的 Discord 元件與發送（M3-24）。規則與文案在 services/daily_briefing.py。

元件都是 DynamicItem（custom_id 帶日期），bot 重啟後舊早報上的選單與按鈕仍可用。
這個模組不是 cog：由 cogs/reminders.py 呼叫 send_daily_briefings()，元件在 bot.py 註冊。
"""
import logging
from datetime import date, datetime

import discord

import config
from cogs import ui_text
from services import audit, daily_briefing, notification_log, ops_briefing, repositories, task_notifications, task_query
from services.notion_links import strip_notion_links

logger = logging.getLogger(__name__)


def _day_code(day: date) -> str:
    return day.strftime("%Y%m%d")


def _deadline_fn(now: datetime):
    return lambda task: ui_text.deadline_text(task, now)


class BriefingDetailSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"briefing_detail:(?P<day>\d{8})",
):
    """「看某一筆的詳細」：選了跳出只有自己看得到的完整內容，私訊裡不會多一則。"""

    def __init__(self, day: str, options: list[discord.SelectOption] | None = None):
        self.options = options or [discord.SelectOption(label="（沒有任務）", value="none")]
        super().__init__(
            discord.ui.Select(
                placeholder="看某一筆的詳細…",
                options=self.options,
                custom_id=f"briefing_detail:{day}",
                min_values=1,
                max_values=1,
            )
        )
        self.day = day

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match):
        return cls(match.group("day"), list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        # 2026-10-20 QA：原本每看一筆就另跳一則 ephemeral，看兩筆以上就往下疊，「我都看到了」被推到上面。
        # 改成展開在早報同一則裡（embed），再選別筆就替換；選單與按鈕永遠在最下面。編輯訊息不會跳通知。
        await interaction.response.defer()
        values = (interaction.data or {}).get("values") or []
        task_id = values[0] if values else ""
        task = await repositories.get_task(task_id) if task_id and task_id != "none" else None
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if task is None or artist is None or artist.page_id not in task.artist_page_ids:
            await interaction.followup.send("❌ 找不到這筆任務，可能已經結案。用 `/我的任務` 看最新清單。", ephemeral=True)
            return
        body = task.description
        if not body:
            try:
                body = await repositories.get_task_page_body(task.page_id)
            except Exception:
                logger.exception("早報讀取任務內頁失敗：%s", task.page_id)
                body = ""
        now = datetime.now(config.get_tenant().tz)
        detail = task_query.task_detail_text(task, deadline_text=ui_text.deadline_text(task, now), body=body)
        embed = discord.Embed(description=detail[:4000], color=0x5865F2)
        embed.set_footer(text="再選別筆會換成那一筆；看完按「我都看到了」")
        options = [
            discord.SelectOption(label=o.label, value=o.value, description=o.description, default=o.value == task_id)
            for o in self.options
        ]
        view = discord.ui.View(timeout=None)
        view.add_item(BriefingDetailSelect(self.day, options))
        view.add_item(BriefingAckButton(self.day))
        await interaction.edit_original_response(embed=embed, view=view)


class BriefingAckButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"briefing_ack:(?P<day>\d{8})",
):
    """「👌 我都看到了」：早報收成一行，每筆任務的「提醒確認」各寫一行。"""

    def __init__(self, day: str):
        super().__init__(
            discord.ui.Button(
                label="我都看到了",
                emoji="👌",
                style=discord.ButtonStyle.success,
                custom_id=f"briefing_ack:{day}",
            )
        )
        self.day = day

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(match.group("day"))

    async def callback(self, interaction: discord.Interaction) -> None:
        state = notification_log.shared()
        now = datetime.now(config.get_tenant().tz)
        if self.day != _day_code(now.date()):
            await interaction.response.send_message("ℹ️ 這是之前的早報，請看最新的那一則。", ephemeral=True)
            return
        entry = daily_briefing.briefing_for(state, interaction.user.id)
        if not entry or entry.get("day") != self.day:
            # 狀態檔沒有這則（例如狀態遺失、或是手動補發的早報）：任務清單直接從訊息的選單讀
            entry = await _entry_from_message(interaction, self.day)
            if entry is None:
                await interaction.response.send_message("❌ 讀不到這則早報的任務，請用 `/我的任務`。", ephemeral=True)
                return
            daily_briefing.remember_briefing(state, interaction.user.id, entry)
        day = datetime.strptime(self.day, "%Y%m%d").date()
        task_ids = list(entry.get("task_ids") or [])
        # 先改訊息（Discord 3 秒內要回應），再寫 Notion。編輯訊息不會跳通知。
        await interaction.response.edit_message(
            content=daily_briefing.collapsed_text(day, len(task_ids), now), view=None, embed=None,
        )
        artist_page_id = entry.get("artist_page_id", "")
        artist_name = entry.get("artist_name", "")
        daily_briefing.record_ack(state, task_ids, artist_page_id, day)
        entry["acked_at"] = now.isoformat()
        notification_log.persist()
        line = task_notifications.format_reminder_ack_line(daily_briefing.ACK_STAGE, now, artist_name)
        for task_id in task_ids:
            try:
                await repositories.append_reminder_ack_line(task_id, line)
            except Exception:
                logger.exception("早報確認寫入 Notion 失敗：%s", task_id)
        await audit.log_event(
            event_name=f"藝人確認每日早報｜{artist_name}",
            action_type="更新", operator=artist_name, discord_user_id=str(interaction.user.id),
            target_db="Tasks", after=f"{day:%m/%d} 早報｜{len(task_ids)} 筆",
        )


async def _entry_from_message(interaction: discord.Interaction, day: str) -> dict | None:
    """從早報訊息的「看某一筆的詳細」選單撈出任務 ID；選單選項就是這則早報列的任務。"""
    message = getattr(interaction, "message", None)
    task_ids: list[str] = []
    for row in getattr(message, "components", None) or []:
        for component in getattr(row, "children", None) or []:
            if getattr(component, "custom_id", "") == f"briefing_detail:{day}":
                task_ids = [option.value for option in component.options if option.value != "none"]
    artist = await repositories.find_artist_by_discord_id(interaction.user.id)
    if not task_ids or artist is None:
        return None
    return {
        "day": day,
        "channel_id": getattr(getattr(message, "channel", None), "id", None),
        "message_id": getattr(message, "id", None),
        "task_ids": task_ids,
        "artist_page_id": artist.page_id,
        "artist_name": artist.name,
    }


def briefing_view(day: date, items: list[daily_briefing.BriefingItem], now: datetime) -> discord.ui.View:
    deadline_text = _deadline_fn(now)
    options = [
        discord.SelectOption(
            label=strip_notion_links(item.task.name)[:100] or "未命名任務",
            value=item.task.page_id,
            description=daily_briefing.option_description(item, deadline_text),
        )
        for item in items
    ]
    view = discord.ui.View(timeout=None)
    view.add_item(BriefingDetailSelect(_day_code(day), options))
    view.add_item(BriefingAckButton(_day_code(day)))
    return view


async def _delete_previous(client: discord.Client, entry: dict | None) -> None:
    if not entry or not entry.get("channel_id") or not entry.get("message_id"):
        return
    try:
        channel = client.get_partial_messageable(int(entry["channel_id"]))
        await channel.get_partial_message(int(entry["message_id"])).delete()
    except discord.NotFound:
        pass
    except Exception:
        logger.warning("刪除前一天的早報失敗：%s", entry.get("message_id"))


async def send_daily_briefings(client: discord.Client, now: datetime) -> dict:
    """每位藝人一則早報；沒有要列的就只刪掉昨天那則。回傳統計供 audit。"""
    state = notification_log.shared()
    tasks = await repositories.get_active_tasks()
    artist_map = await repositories.get_artist_map()
    queued = daily_briefing.queued_task_ids(state)
    daily_briefing.prune_acks(state, {task.page_id for task in tasks})
    acks = state.get("briefing_acks", {})
    day = now.date()
    sent = cleared = 0
    sent_per_task: dict[str, int] = {}
    for artist in artist_map.values():
        if not artist.discord_user_id:
            continue
        previous = daily_briefing.briefing_for(state, artist.discord_user_id)
        items = daily_briefing.build_items(artist.page_id, tasks, queued, acks)
        if previous and previous.get("day") == _day_code(day):
            continue  # 今天已發過（重啟後同一天不重發）
        if previous and previous.get("day") and not previous.get("acked_at") and previous.get("task_ids"):
            # 昨天那則沒按「我都看到了」→ 給營運早報的「昨天的早報沒看」（M3-25）
            ops_briefing.record_unread(state, previous["day"], previous.get("artist_name") or artist.name)
        await _delete_previous(client, previous)
        if not items:
            if previous:
                state.get("briefings", {}).pop(str(artist.discord_user_id), None)
                cleared += 1
            continue
        try:
            user = await client.fetch_user(int(artist.discord_user_id))
            message = await user.send(
                daily_briefing.briefing_text(day, items, _deadline_fn(now)),
                view=briefing_view(day, items, now),
            )
        except Exception:
            logger.exception("每日早報發送失敗：%s", artist.name)
            continue
        daily_briefing.remember_briefing(state, artist.discord_user_id, {
            "day": _day_code(day),
            "channel_id": message.channel.id,
            "message_id": message.id,
            "task_ids": [item.task.page_id for item in items],
            "artist_page_id": artist.page_id,
            "artist_name": artist.name,
        })
        sent += 1
        for item in items:
            sent_per_task[item.task.page_id] = sent_per_task.get(item.task.page_id, 0) + 1
    daily_briefing.clear_queue(state)
    notification_log.persist()
    # 送出也記一行，控方才分得出「還沒發」和「發了沒人按」（M3-18 的語意不變）
    for task_id, count in sent_per_task.items():
        try:
            await repositories.append_reminder_ack_line(
                task_id, task_notifications.format_reminder_sent_line(daily_briefing.ACK_STAGE, now, count),
            )
        except Exception:
            logger.exception("早報送出紀錄寫入失敗：%s", task_id)
    return {"sent": sent, "cleared": cleared, "tasks": len(sent_per_task)}
