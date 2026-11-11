"""藝人直接傳到 Drive 的作業：小律每天 12:00、21:00 找出來，私訊確認後補登交件（M4-23）。

選單和按鈕是 DynamicItem：私訊會留著，bot 重開後按了也有回應。
補登跟一般交件走同一個 `record_folder_submission`，Notion 欄位一模一樣；提交時間用藝人實際上傳的時間。
"""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config
from cogs import submissions, ui_text
from services import audit, drive_api, drive_archive, repositories, stray_uploads

logger = logging.getLogger(__name__)

EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)  # 搬進來的檔案都算（它們的上傳時間比新資料夾早）
_KEY = r"(?P<key>[fl][\w-]{1,80})"


def _folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


class StrayTaskSelect(discord.ui.DynamicItem[discord.ui.Select], template=rf"stray_pick:{_KEY}"):
    def __init__(self, key: str, options: list[discord.SelectOption] | None = None):
        self.options = options or [discord.SelectOption(label="（沒有可以交的作業）", value="none")]
        super().__init__(discord.ui.Select(
            placeholder="選擇要交的作業…", options=self.options,
            custom_id=f"stray_pick:{key}", min_values=1, max_values=1,
        ))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match):
        return cls(match["key"], list(item.options))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        if not values or values[0] == "none":
            await interaction.response.defer()
            return
        await interaction.response.defer()
        text, done = await adopt(interaction.client, interaction.user.id, self.key, values[0])
        if done:
            await interaction.edit_original_response(content=text, view=None)
        else:
            await interaction.followup.send(text)


class StrayIgnoreButton(discord.ui.DynamicItem[discord.ui.Button], template=rf"stray_ignore:{_KEY}"):
    def __init__(self, key: str):
        super().__init__(discord.ui.Button(
            label="不是作業，不用交", style=discord.ButtonStyle.secondary, custom_id=f"stray_ignore:{key}",
        ))
        self.label = "不是作業，不用交"
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction) -> None:
        entry = stray_uploads.get_group(self.key)
        if entry and str(entry.get("discord_user_id")) != str(interaction.user.id):
            await interaction.response.send_message("❌ 這不是你的檔案。")
            return
        stray_uploads.update_group(
            self.key, status="ignored", handled_at=datetime.now(timezone.utc).isoformat(),
        )
        await interaction.response.edit_message(
            content="好，這些檔案小律就不登記成交件了。之後要交作業，記得用 `/我的任務` → 提交任務喔～",
            view=None,
        )


def stray_view(key: str, task_list: list, now: datetime) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if task_list:
        options = [
            discord.SelectOption(
                label=task.name[:100],
                value=task.page_id,
                description=f"{task.status or '進行中'}｜{ui_text.deadline_text(task, now)}"[:100],
            )
            for task in task_list
        ]
        view.add_item(StrayTaskSelect(key, options))
    view.add_item(StrayIgnoreButton(key))
    return view


async def adopt(client, discord_user_id: int, key: str, task_page_id: str) -> tuple[str, bool]:
    """把這組檔案交到選的作業：開標準資料夾 → 整組搬進去 → 照一般交件寫 Notion。回傳（給藝人的話, 是否完成）。"""
    entry = stray_uploads.get_group(key)
    if not entry or entry.get("status") == "submitted":
        return "這些檔案已經交過了喔。", True
    if str(entry.get("discord_user_id")) != str(discord_user_id):
        return "❌ 這不是你的檔案。", False
    tz = config.get_tenant().tz
    now = datetime.now(tz)
    artist = await repositories.find_artist_by_discord_id(discord_user_id)
    task = await repositories.get_task(task_page_id)
    if artist is None or task is None or artist.page_id not in task.artist_page_ids \
            or not repositories.is_task_submittable(task):
        return "❌ 這筆作業現在不能交了，請選別筆，或直接跟公司說。", False

    moved = False
    folder_url = ""
    files: list[dict] = []
    try:
        target, _ = await submissions._resolve_submission_target(task, artist, now)
        if target is None:
            raise RuntimeError("找不到這位藝人的 Drive 專區")
        base_folder_id, segments = target
        ver, _path = await submissions._ensure_next_version_folder(
            base_folder_id=base_folder_id, parent_path=segments, item_name=task.name, created_at=now,
        )
        if entry["kind"] == "folder":
            await drive_api.move_item(entry["folder_id"], ver["id"])
        else:
            for file_id in entry.get("file_ids") or []:
                await drive_api.move_item(file_id, ver["id"])
        moved = True
        folder_url = ver.get("webViewLink") or _folder_url(ver["id"])
        files = await drive_api.list_recent_files(ver["id"], EPOCH)
    except Exception:
        logger.exception("補登時搬到標準資料夾失敗，改用原本的位置：%s", key)
        moved = False
        folder_url = _folder_url(entry["folder_id"])
        try:
            if entry["kind"] == "folder":
                files = await drive_api.list_recent_files(entry["folder_id"], EPOCH)
            else:
                files = await drive_api.get_files(entry.get("file_ids") or [])
        except Exception:
            logger.exception("補登時讀不到檔案：%s", key)
            files = []
    if not files:
        return "❌ 小律找不到那些檔案了（可能被搬走或刪掉），請用 `/我的任務` → 提交任務重新交。", False

    origin = f"{entry.get('location', '')}{entry.get('name', '')}"
    note = (
        f"小律在 Drive 找到，{artist.name} {now:%m/%d} 確認補登；原本放在：{origin}"
        + ("" if moved else "（搬到標準資料夾失敗，檔案留在原處）")
    )
    submitted_at = stray_uploads.local_time(entry.get("last_uploaded") or "", tz) or now
    try:
        recorded = await submissions.record_folder_submission(
            client, task, artist, files, folder_url,
            discord_user_id=discord_user_id, submitted_at=submitted_at, note=note,
            audit_title="提交任務（Drive 補登）",
            reviewer_title="📥 **新提交待審核（小律在 Drive 找到、補登）**",
        )
    except Exception:
        logger.exception("補登寫入 Notion 失敗：%s", key)
        return "❌ 檔案小律看到了，但記到 Notion 時失敗了，請稍後再選一次，或跟公司說。", False
    stray_uploads.update_group(
        key, status="submitted", handled_at=datetime.now(timezone.utc).isoformat(),
        task_page_id=task.page_id, task_name=task.name, submission_url=recorded["url"], reported=False,
    )
    where = f"\n檔案已經放進這筆作業的資料夾：{folder_url}" if moved else "\n檔案留在原本的位置。"
    return (
        f"✅ 已經幫你交到「{task.name}」\n"
        f"{recorded['count']} 個檔案，任務改成「待審核」，公司會收到通知。{where}\n\n"
        "下次交作業，用 `/我的任務` → 提交任務會更快喔～"
    ), True


class DriveWatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.watch_loop.start()

    def cog_unload(self):
        self.watch_loop.cancel()

    @tasks.loop(minutes=30)
    async def watch_loop(self):
        if not drive_api.is_configured():
            return
        now = datetime.now(config.get_tenant().tz)
        slot = stray_uploads.scan_slot(now)
        if slot is None:
            return
        try:
            await self.run_scan(now)
        except Exception:
            logger.exception("Drive 散檔掃描失敗")
        stray_uploads.set_meta("last_slot", slot)

    @watch_loop.before_loop
    async def before_watch(self):
        await self.bot.wait_until_ready()

    async def run_scan(self, now: datetime) -> int:
        """掃每位藝人的專區，該問的私訊。回傳私訊了幾組。"""
        asked = 0
        now_utc = now.astimezone(timezone.utc)
        for artist in await repositories.get_active_artists():
            zone_id = drive_archive.extract_drive_folder_id(artist.drive_folder_url or "")
            if not zone_id:
                continue
            try:
                groups = await asyncio.to_thread(
                    stray_uploads.scan_zone_sync, drive_api._get_service(), zone_id, artist.name,
                )
            except Exception:
                logger.exception("掃描藝人 Drive 專區失敗：%s", artist.name)
                continue
            stray_uploads.forget_missing(artist.page_id, {group.key for group in groups})
            for group in groups:
                if stray_uploads.needs_asking(group, now_utc):
                    await self._ask(artist, group, now)
                    asked += 1
        logger.info("Drive 散檔掃描完成：私訊 %d 組", asked)
        return asked

    async def _ask(self, artist, group: stray_uploads.StrayGroup, now: datetime) -> None:
        try:
            artist_tasks = await repositories.get_discord_tasks_for_artist(artist.page_id)
        except Exception:
            logger.exception("讀取藝人任務失敗：%s", artist.name)
            artist_tasks = []
        options = stray_uploads.suggest_tasks(artist_tasks, group.last_uploaded, hint=group.name)
        tz = config.get_tenant().tz
        status = "asked" if options else "no_task"
        try:
            user = await self.bot.fetch_user(int(artist.discord_user_id))
            await user.send(stray_uploads.ask_text(group, tz, bool(options)), view=stray_view(group.key, options, now))
        except (discord.HTTPException, ValueError):  # ValueError：Discord User ID 不是數字
            logger.warning("私訊藝人失敗（Drive 散檔）：%s", artist.name, exc_info=True)
            status = "dm_failed"
        stray_uploads.update_group(
            group.key, status=status, artist_page_id=artist.page_id, artist_name=artist.name,
            discord_user_id=str(artist.discord_user_id), kind=group.kind, folder_id=group.folder_id,
            name=group.name, label=group.label(), location=group.location, file_ids=group.file_ids,
            count=group.count, total_mb=group.total_mb, first_uploaded=group.first_uploaded,
            last_uploaded=group.last_uploaded, asked_at=now.isoformat(), reported=False,
        )
        await audit.log_event(
            event_name=f"Drive 直接上傳待確認｜{artist.name}｜{group.name}"[:200],
            action_type="提醒", operator="System", source="Google Drive",
            target_db="Google Drive", target_url=_folder_url(group.folder_id),
            after=f"{group.count} 個檔案｜{status}",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DriveWatchCog(bot))
