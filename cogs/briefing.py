"""每日早報的 Discord 元件與發送（M3-24）。規則與文案在 services/daily_briefing.py。

元件都是 DynamicItem（custom_id 帶日期），bot 重啟後舊早報上的選單與按鈕仍可用。
這個模組不是 cog：由 cogs/reminders.py 呼叫 send_daily_briefings()，元件在 bot.py 註冊。
"""
import logging
from datetime import date, datetime

import discord

import config
from cogs import ui_text
from services import audit, daily_briefing, notification_log, ops_briefing, repositories, task_bundle, task_notifications, task_query
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
        if task is None or not await _can_view(interaction.user.id, task):
            await interaction.followup.send("❌ 找不到這筆任務，可能已經結案。用 `/我的任務` 看最新清單。", ephemeral=True)
            return
        body = task.description  # get_task 已經讀了任務內頁整頁
        now = datetime.now(config.get_tenant().tz)
        detail = task_query.task_detail_text(task, deadline_text=ui_text.deadline_text(task, now), body=body)
        embed = discord.Embed(description=detail[:4000], color=0x5865F2)
        embed.set_footer(text="再選別筆會換成那一筆；看完按「我都看到了」")
        options = [
            discord.SelectOption(label=o.label, value=o.value, description=o.description, default=o.value == task_id)
            for o in self.options
        ]
        # 同仁早報本來就沒有「我都看到了」：看詳細時也不要冒出來（照原訊息有沒有這顆判斷）
        message = getattr(interaction, "message", None)
        artist_briefing = message is None or _has_ack_button(message, self.day)
        view = discord.ui.View(timeout=None)
        view.add_item(BriefingDetailSelect(self.day, options))
        if artist_briefing:
            view.add_item(BriefingAckButton(self.day))
            # 收到之後仍可回報問題（M3-27）：藝人每天看早報，展開某一筆時就能說這筆不行。
            # 一律顯示（2026-10-23 UAT）：已交件、已回報的按了會說明原因，不再讓按鈕無聲消失
            entry = daily_briefing.briefing_for(notification_log.shared(), interaction.user.id) or {}
            if task.page_id not in (entry.get("reported") or []):
                from cogs.tasks_artist import TaskConcernButton

                view.add_item(TaskConcernButton(task.page_id, "這筆有問題"))
        await interaction.edit_original_response(embed=embed, view=view)


def _has_ack_button(message, day: str) -> bool:
    for row in getattr(message, "components", None) or []:
        for component in getattr(row, "children", None) or []:
            if getattr(component, "custom_id", "") == f"briefing_ack:{day}":
                return True
    return False


async def _can_view(discord_user_id: int, task: repositories.TaskItem) -> bool:
    """關聯藝人或相關同仁才看得到這筆任務的詳細。"""
    artist = await repositories.find_artist_by_discord_id(discord_user_id)
    if artist is not None and artist.page_id in task.artist_page_ids:
        return True
    if not task.staff_page_ids:
        return False
    people = await repositories.get_people_items_by_ids(task.staff_page_ids)
    return any(person.discord_user_id == str(discord_user_id) for person in people)


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
        await ack_briefing(interaction, self.day, getattr(interaction, "message", None), edit_via_response=True)


async def ack_briefing(interaction: discord.Interaction, day_code: str, message, *, edit_via_response: bool) -> bool:
    """「我都看到了」：早報收成一行，每筆任務的「提醒確認」各寫一行。

    早報上的按鈕走 edit_via_response（Discord 3 秒內要回應，直接改這則）；早報每一筆都回報了自動算已讀時
    （M3-28）已經 defer 過，改的是早報本身（message）。回報過問題的那幾筆不記看過。
    """
    state = notification_log.shared()
    now = datetime.now(config.get_tenant().tz)

    async def say(text: str) -> None:
        if edit_via_response:
            await interaction.response.send_message(text, ephemeral=True)
        else:
            await interaction.followup.send(text, ephemeral=True)

    if day_code != _day_code(now.date()):
        await say("ℹ️ 這是之前的早報，請看最新的那一則。")
        return False
    entry = daily_briefing.briefing_for(state, interaction.user.id)
    if not entry or entry.get("day") != day_code:
        # 狀態檔沒有這則（例如狀態遺失、或是手動補發的早報）：任務清單直接從訊息的選單讀
        entry = await _entry_from_message(interaction, day_code, message)
        if entry is None:
            await say("❌ 讀不到這則早報的任務，請用 `/我的任務`。")
            return False
        daily_briefing.remember_briefing(state, interaction.user.id, entry)
    day = datetime.strptime(day_code, "%Y%m%d").date()
    reported = set(entry.get("reported") or [])
    all_ids = list(entry.get("task_ids") or [])
    task_ids = [tid for tid in all_ids if tid not in reported]
    # 先改訊息（Discord 3 秒內要回應），再寫 Notion。編輯訊息不會跳通知。
    content = daily_briefing.collapsed_text(day, len(all_ids), now, reported=len(all_ids) - len(task_ids))
    if edit_via_response:
        await interaction.response.edit_message(content=content, view=None, embed=None)
    else:
        try:
            await message.edit(content=content, view=None, embed=None)
        except Exception:
            logger.warning("早報無法收合：%s", day_code)
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
    return True


def _briefing_day(message) -> str | None:
    """這則是藝人早報的話，回傳 custom_id 裡的日期（有「我都看到了」才算；同仁早報沒有）。"""
    for row in getattr(message, "components", None) or []:
        for component in getattr(row, "children", None) or []:
            custom_id = str(getattr(component, "custom_id", "") or "")
            if custom_id.startswith("briefing_ack:"):
                return custom_id.split(":", 1)[1]
    return None


def is_briefing_message(message) -> bool:
    return _briefing_day(message) is not None


async def mark_reported(interaction: discord.Interaction, message, task: repositories.TaskItem) -> int:
    """早報裡某一筆回報了問題：那一行劃掉標「已回報」（M3-28）。回傳早報裡還有幾筆沒回報。

    每一筆都回報了就直接算已讀收合，回 0。
    """
    day_code = _briefing_day(message)
    state = notification_log.shared()
    entry = daily_briefing.briefing_for(state, interaction.user.id)
    if not entry or entry.get("day") != day_code:
        entry = await _entry_from_message(interaction, day_code, message)
        if entry is None:
            return 0
        daily_briefing.remember_briefing(state, interaction.user.id, entry)
    reported = list(entry.get("reported") or [])
    if task.page_id not in reported:
        reported.append(task.page_id)
    entry["reported"] = reported
    notification_log.persist()
    remaining = [tid for tid in entry.get("task_ids") or [] if tid not in reported]
    if not remaining:
        await ack_briefing(interaction, day_code, message, edit_via_response=False)
        return 0
    options = []
    for row in getattr(message, "components", None) or []:
        for component in getattr(row, "children", None) or []:
            if getattr(component, "custom_id", "") == f"briefing_detail:{day_code}":
                options = [discord.SelectOption(label=o.label, value=o.value, description=o.description)
                           for o in component.options]
    view = discord.ui.View(timeout=None)
    view.add_item(BriefingDetailSelect(day_code, options))
    view.add_item(BriefingAckButton(day_code))
    await message.edit(content=daily_briefing.mark_reported_line(message.content, task.name), embed=None, view=view)
    return len(remaining)


async def _entry_from_message(interaction: discord.Interaction, day: str, message=None) -> dict | None:
    """從早報訊息的「看某一筆的詳細」選單撈出任務 ID；選單選項就是這則早報列的任務。"""
    if message is None:
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


def briefing_view(
    day: date, items: list[daily_briefing.BriefingItem], now: datetime, *, with_ack: bool = True,
) -> discord.ui.View:
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
    if with_ack:
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


def _options_from_items(items: list[daily_briefing.BriefingItem], now: datetime) -> list[dict]:
    deadline_text = _deadline_fn(now)
    return [
        {"label": strip_notion_links(item.task.name)[:100] or "未命名任務", "value": item.task.page_id,
         "description": daily_briefing.option_description(item, deadline_text)}
        for item in items
    ]


def reminders_view(day_code: str, options: list[dict]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(BriefingDetailSelect(day_code, [discord.SelectOption(**o) for o in options]))
    view.add_item(BriefingAckButton(day_code))
    return view


async def _clear_old_task_messages(client, user_id: str) -> None:
    """早上發新的一則前，清掉這位藝人之前的任務通知（M3-29 第五版）。

    收成一行的「已收到／已回報」是處理完的；還沒收的會併進今天這則。私訊裡只留今天要看的。
    只清得到 bot 有記錄的訊息。
    """
    from cogs.tasks_artist import clear_old_notifications

    state = notification_log.shared()
    task_ids = {tid for tid, per_user in state.get("messages", {}).items() if str(user_id) in per_user}
    if task_ids:
        await clear_old_notifications(client, task_ids, user_id)


async def show_reminders(message, user_id, bundle_id: str, received: list, reported: list) -> bool:
    """第一段（先確認還沒收到的任務）處理完：同一則換成今天的提醒（M3-29 第五版）。

    回傳 False＝狀態檔沒有這則早報（不是早報的第一段），呼叫端照一般合併通知收合。
    """
    state = notification_log.shared()
    entry = daily_briefing.briefing_for(state, user_id)
    if not entry or entry.get("bundle_id") != bundle_id:
        return False
    now = datetime.now(config.get_tenant().tz)
    head = daily_briefing.handled_line(len(received), len(reported))
    entry["stage"] = 2
    if not entry.get("reminder_text"):
        # 今天沒有提醒：原地收合就好，也不用再按「我都看到了」
        day = datetime.strptime(entry["day"], "%Y%m%d").date()
        entry["acked_at"] = now.isoformat()
        notification_log.persist()
        await message.edit(content=f"☀️ {day.month}/{day.day} 今天的任務提醒\n{head}", embed=None, view=None)
        return True
    lines = entry["reminder_text"].split("\n")
    content = "\n".join([lines[0], head, *lines[1:]])[:2000]
    view = reminders_view(entry["day"], entry.get("options") or [])
    # 2026-10-23 Johnny：原地換的話，提醒長在按鈕上方、「我都看到了」被推到畫面外，藝人不會發現還要按。
    # 改成第一段刪掉、提醒以新訊息出現在最下面（視線本來就在那），私訊裡還是只有一則。
    try:
        new_message = await message.channel.send(content, view=view)
    except Exception:
        logger.warning("早報第二段改發新訊息失敗，改成原地換：%s", entry.get("message_id"))
        notification_log.persist()
        await message.edit(content=content, embed=None, view=view)
        return True
    try:
        await message.delete()
    except discord.NotFound:
        pass  # 第一段已不存在，新的提醒就是私訊裡唯一可操作的早報
    except Exception:
        logger.warning("早報第一段刪除失敗，回退原訊息：%s", getattr(message, "id", None))
        try:
            await new_message.delete()
        except Exception:
            # 兩邊都刪不掉時，至少拿掉舊訊息的按鈕，避免兩則都能操作。
            logger.warning("早報第二段回收失敗，停用第一段按鈕：%s", getattr(new_message, "id", None))
            try:
                await message.edit(content="↪️ 今天的任務提醒已移到下方。", embed=None, view=None)
            except Exception:
                logger.warning("早報第一段停用失敗：%s", getattr(message, "id", None))
            entry["channel_id"] = new_message.channel.id
            entry["message_id"] = new_message.id
            notification_log.persist()
            return True
        # 新訊息已回收，安全地在原訊息顯示第二段；私訊仍只有一則。
        await message.edit(content=content, embed=None, view=view)
        entry["channel_id"] = message.channel.id
        entry["message_id"] = message.id
        notification_log.persist()
        return True
    entry["channel_id"] = new_message.channel.id
    entry["message_id"] = new_message.id
    notification_log.persist()
    return True


async def send_daily_briefings(client: discord.Client, now: datetime, *, final: bool = True) -> dict:
    """每位藝人一則早報；沒有要列的就只刪掉昨天那則。回傳統計供 audit。

    今天已發過的人會跳過，所以失敗後重跑只補發沒送到的人（M3-11）。`final=False` 時有人失敗就
    保留「快到期」佇列給下一次重試；最後一次（或全部成功）才清掉。

    M3-29 第五版（Johnny 2026-10-23）：早上只有一則、一次只有一種按鈕。有還沒收到的任務時，
    先只顯示「先確認：還沒收到的任務」（全部收到／某一筆有問題），處理完同一則才換成今天的提醒。
    """
    from cogs.tasks_artist import bundle_options, bundle_view

    state = notification_log.shared()
    tasks = await repositories.get_active_tasks()
    artist_map = await repositories.get_artist_map()
    queued = daily_briefing.queued_task_ids(state)
    daily_briefing.prune_acks(state, {task.page_id for task in tasks})
    acks = state.get("briefing_acks", {})
    day = now.date()
    sent = cleared = failed = 0
    sent_per_task: dict[str, int] = {}
    for artist in artist_map.values():
        if not artist.discord_user_id:
            continue
        uid = str(artist.discord_user_id)
        previous = daily_briefing.briefing_for(state, uid)
        items, unreceived = daily_briefing.split_unreceived(
            daily_briefing.build_items(artist.page_id, tasks, queued, acks, now)
        )
        if previous and previous.get("day") == _day_code(day):
            continue  # 今天已發過（重啟後同一天不重發）
        if (previous and previous.get("day") and not previous.get("acked_at") and previous.get("task_ids")
                and previous.get("role") != "staff"):
            # 昨天那則沒按「我都看到了」→ 給營運早報的「昨天的早報沒看」（M3-25）
            ops_briefing.record_unread(state, previous["day"], previous.get("artist_name") or artist.name)
        await _delete_previous(client, previous)
        if not items and not unreceived:
            if previous:
                state.get("briefings", {}).pop(uid, None)
                cleared += 1
            continue
        await _clear_old_task_messages(client, uid)
        reminder_text = daily_briefing.briefing_text(day, items, _deadline_fn(now)) if items else ""
        options = _options_from_items(items, now)
        bundle_id = None
        try:
            user = await client.fetch_user(int(artist.discord_user_id))
            if unreceived:
                bundle_id = task_bundle.new_bundle_id()
                message = await user.send(
                    task_bundle.bundle_text(task_bundle.KIND_BRIEFING, unreceived, _deadline_fn(now)),
                    view=bundle_view(bundle_id, bundle_options(unreceived)),
                )
            else:
                message = await user.send(reminder_text, view=reminders_view(_day_code(day), options))
        except Exception:
            logger.exception("每日早報發送失敗：%s", artist.name)
            failed += 1
            continue
        if bundle_id:
            ids = [task.page_id for task in unreceived]
            task_bundle.remember_bundle(state, bundle_id, uid, task_bundle.KIND_BRIEFING, ids, now)
            for task_id in ids:
                notification_log.record_message(state, task_id, uid, message.channel.id, message.id, now, bundle_id=bundle_id)
        daily_briefing.remember_briefing(state, uid, {
            "day": _day_code(day),
            "channel_id": message.channel.id,
            "message_id": message.id,
            "task_ids": [item.task.page_id for item in items],
            "artist_page_id": artist.page_id,
            "artist_name": artist.name,
            "bundle_id": bundle_id,
            "stage": 1 if bundle_id else 2,
            "reminder_text": reminder_text,
            "options": options,
        })
        sent += 1
        for item in items:
            sent_per_task[item.task.page_id] = sent_per_task.get(item.task.page_id, 0) + 1
    staff_sent, staff_failed = await _send_staff_briefings(client, state, tasks, queued, day, now)
    failed += staff_failed
    if not failed or final:
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
    return {"sent": sent, "cleared": cleared, "tasks": len(sent_per_task), "staff_sent": staff_sent, "failed": failed}


async def _send_staff_briefings(
    client, state: dict, tasks, queued: set[str], day: date, now: datetime,
) -> tuple[int, int]:
    """相關同仁每人每天一則（M3-25 第三階段）；取代逐階段的同仁提醒。沒有「我都看到了」。回傳（送出, 失敗）。"""
    staff_ids = sorted({pid for task in tasks if task.discord_visible for pid in task.staff_page_ids})
    if not staff_ids:
        return 0, 0
    try:
        people = await repositories.get_people_items_by_ids(staff_ids)
    except Exception:
        logger.exception("同仁早報：讀取 People 失敗")
        return 0, 1
    sent = failed = 0
    for person in people:
        if not person.discord_user_id:
            continue
        previous = daily_briefing.briefing_for(state, person.discord_user_id)
        if previous and previous.get("day") == _day_code(day):
            continue
        await _delete_previous(client, previous)
        items = daily_briefing.build_staff_items(person.page_id, tasks, queued, now)
        if not items:
            if previous:
                state.get("briefings", {}).pop(str(person.discord_user_id), None)
            continue
        try:
            user = await client.fetch_user(int(person.discord_user_id))
            message = await user.send(
                daily_briefing.briefing_text(day, items, _deadline_fn(now), footer=daily_briefing.STAFF_FOOTER),
                view=briefing_view(day, items, now, with_ack=False),
            )
        except Exception:
            logger.exception("同仁早報發送失敗：%s", person.name)
            failed += 1
            continue
        daily_briefing.remember_briefing(state, person.discord_user_id, {
            "day": _day_code(day), "channel_id": message.channel.id, "message_id": message.id,
            "task_ids": [item.task.page_id for item in items], "role": "staff", "artist_name": person.name,
        })
        sent += 1
    return sent, failed
