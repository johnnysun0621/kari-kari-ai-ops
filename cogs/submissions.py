"""P0-3 作業提交：/提交任務（連結／文字先行；檔案上傳等 Drive 憑證後開通）。"""
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard, tasks_artist
from services import audit, repositories

logger = logging.getLogger(__name__)

LINK_PREFIXES = ("http://", "https://")


def submission_type_for_input(link_url: str, content: str) -> str | None:
    """依實際填寫內容決定 Submissions.提交類型；兩者皆空回 None（呼叫端擋下）。"""
    if link_url and content:
        return "混合"
    if link_url:
        return "Drive 連結"
    if content:
        return "文字"
    return None


def is_valid_link(link_url: str) -> bool:
    return link_url.startswith(LINK_PREFIXES) and " " not in link_url


def submit_hint_for_task(task: repositories.TaskItem) -> str:
    """Modal 開頭給藝人的提示：依任務的提交方式引導，檔案任務先導向雲端連結。"""
    method = task.submit_method
    if method == "檔案":
        return "此任務原定上傳檔案；檔案上傳尚未開通，請先貼雲端連結（Drive／雲端硬碟）。"
    if method == "Drive 連結":
        return "請貼上作品的雲端連結（記得開啟共用權限）。"
    if method == "文字":
        return "請直接填寫文字內容。"
    return "可貼雲端連結、填文字內容，或兩者都填。"


class SubmissionModal(discord.ui.Modal):
    """提交表單：連結＋文字皆選填，但至少要填一項。"""

    def __init__(self, task: repositories.TaskItem):
        super().__init__(title=f"提交｜{task.name}"[:45])
        self.task = task
        self.link_input = discord.ui.TextInput(
            label="作品連結（選填，Drive／雲端連結）",
            placeholder="https://...",
            required=False,
            max_length=500,
        )
        self.content_input = discord.ui.TextInput(
            label="文字內容／說明（選填）",
            style=discord.TextStyle.paragraph,
            placeholder=submit_hint_for_task(task)[:100],
            required=False,
            max_length=1500,
        )
        self.add_item(self.link_input)
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        link_url = (self.link_input.value or "").strip()
        content = (self.content_input.value or "").strip()

        submit_type = submission_type_for_input(link_url, content)
        if submit_type is None:
            await interaction.followup.send(
                "❌ 連結和文字內容至少要填一項，請再按一次「提交任務」。", ephemeral=True
            )
            return
        if link_url and not is_valid_link(link_url):
            await interaction.followup.send(
                "❌ 連結格式看起來不對（要以 http:// 或 https:// 開頭），請確認後重新提交。",
                ephemeral=True,
            )
            return

        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send(
                "❌ 找不到你的藝人資料，請聯絡管理者確認 Notion 上的 Discord User ID 設定。",
                ephemeral=True,
            )
            return

        try:
            # 先取消舊版最新標記，再建立新提交（順序反了會把新的一起取消）
            replaced = await repositories.mark_previous_submissions_not_latest(self.task.page_id)
            submission_page = await repositories.create_submission(
                self.task, artist,
                submit_type=submit_type, link_url=link_url, content=content,
            )
            latest_url = link_url or submission_page.get("url", "")
            await repositories.set_task_under_review(self.task.page_id, latest_url)
        except Exception:
            logger.exception("建立提交失敗")
            await interaction.followup.send("❌ 寫入 Notion 失敗，請稍後再試或聯絡管理者。", ephemeral=True)
            await audit.log_event(
                event_name=f"提交任務失敗｜{artist.name}｜{self.task.name}",
                action_type="錯誤", operator=artist.name,
                discord_user_id=str(interaction.user.id),
                target_db="Submissions & File Index", result="失敗",
                error_message="create_submission exception（詳見 bot log）",
            )
            return

        await audit.log_event(
            event_name=f"提交任務｜{artist.name}｜{self.task.name}",
            action_type="提交", operator=artist.name,
            discord_user_id=str(interaction.user.id),
            target_db="Submissions & File Index",
            target_url=submission_page.get("url", ""),
            after=f"{submit_type}｜任務轉待審核" + (f"｜取代 {replaced} 筆舊版" if replaced else ""),
        )

        # 通知控方（營運內部）。通知失敗只記 log，不影響藝人的提交確認
        internal = interaction.client.get_channel(config.ADMIN_NOTIFY_CHANNEL_ID)
        link_part = f"\n連結：{link_url}" if link_url else ""
        await channel_guard.try_send(
            internal,
            content=(
                f"📥 **新提交待審核**\n"
                f"任務：{self.task.name}｜{artist.name}｜{submit_type}"
                f"{link_part}\nNotion：{submission_page.get('url', '')}"
            ),
        )

        resubmit_note = f"\n（已取代先前 {replaced} 筆提交，這筆為最新版）" if replaced else ""
        await interaction.followup.send(
            f"✅ 已提交：**{self.task.name}**（{submit_type}）\n"
            f"任務狀態已轉「待審核」，審核結果會再通知你。{resubmit_note}",
            ephemeral=True,
        )


class SubmitTaskSelect(discord.ui.Select):
    def __init__(self, tasks: list[repositories.TaskItem], now: datetime):
        self.task_by_id = {t.page_id: t for t in tasks}
        options = [
            discord.SelectOption(
                label=t.name[:100],
                value=t.page_id,
                description=(
                    f"{t.task_type or '任務'}｜{tasks_artist._deadline_text(t, now)}"
                )[:100],
            )
            for t in tasks[:25]
        ]
        super().__init__(placeholder="選擇要提交的任務…", options=options)

    async def callback(self, interaction: discord.Interaction):
        task = self.task_by_id[self.values[0]]
        # Modal 必須是元件互動的第一個回應
        await interaction.response.send_modal(SubmissionModal(task))


class SubmitTaskView(discord.ui.View):
    def __init__(self, tasks: list[repositories.TaskItem], now: datetime):
        super().__init__(timeout=600)
        self.add_item(SubmitTaskSelect(tasks, now))


class SubmissionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="提交任務", description="提交作業／作品（雲端連結或文字），任務會轉待審核")
    async def submit_task(self, interaction: discord.Interaction):
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
            logger.exception("讀取可提交任務失敗")
            await interaction.followup.send("❌ 讀取任務失敗，請稍後再試。", ephemeral=True)
            return
        submittable = [t for t in tasks if repositories.is_task_submittable(t)]
        if not submittable:
            await interaction.followup.send(
                "目前沒有開放提交的任務。\n"
                "（任務需在 Notion 勾「是否開放提交」且尚未進入審核／結案狀態）",
                ephemeral=True,
            )
            return
        now = datetime.now(config.get_tenant().tz)
        submittable.sort(key=lambda t: (t.end is None, t.end or now))
        await interaction.followup.send(
            f"📤 {artist.name} 可提交的任務（{len(submittable)}）：\n"
            "選擇後會開啟表單，可貼雲端連結或直接填文字。",
            view=SubmitTaskView(submittable, now),
            ephemeral=True,
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("提交指令執行錯誤", exc_info=error)
        msg = "❌ 指令執行出錯，請稍後再試（詳見 bot log）。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SubmissionsCog(bot))
