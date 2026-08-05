"""KariKari AI Ops Bot 進入點：P0-1 排課 MVP。"""
import logging

import discord
from discord.ext import commands

import config
from cogs.scheduling_artist import FillAvailabilityButton, PoolAvailabilityButton
from cogs.tasks_artist import TaskReceiptButton
from services import audit, notion_api, repositories
from web import server as teacher_web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("karikari")

EXTENSIONS = [
    "cogs.scheduling_admin",
    "cogs.scheduling_artist",
    "cogs.tasks_artist",
    "cogs.submissions",
    "cogs.bulletin",
    "cogs.course_qa",
    "cogs.operations_qa",
    "cogs.server_setup",
    "cogs.reminders",
]


class KariKariBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.messages = True
        intents.dm_messages = True
        # Needed for same-channel attachment submission after /提交任務.
        # Discord strips guild message attachments without Message Content Intent.
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.teacher_web_runner = None

    async def setup_hook(self):
        for ext in EXTENSIONS:
            try:
                await self.load_extension(ext)
            except Exception:
                # 單一擴充套件的問題（例如某個 cog 的狀態檔壞了）不該讓整個
                # bot 開不起來——寧可少一個功能，也不要排課／提交全部一起掛。
                logger.exception("擴充套件載入失敗，其餘功能仍會繼續啟動：%s", ext)
        try:
            self.teacher_web_runner = await teacher_web.start(self)
        except Exception:
            logger.exception("老師確認 web 服務啟動失敗，Discord 功能仍會繼續啟動")
        # persistent 按鈕：bot 重啟後公告上的「填寫可用時間」仍可用
        self.add_dynamic_items(FillAvailabilityButton)
        self.add_dynamic_items(PoolAvailabilityButton)
        self.add_dynamic_items(TaskReceiptButton)
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands 已同步到 guild %s", config.GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands 已全域同步（最多等 1 小時生效）")

    async def on_ready(self):
        logger.info("已登入：%s (%s)", self.user, self.user.id)
        # Notion 連線 smoke test：讀 Artists + 寫一筆 Audit Log
        try:
            artists = await repositories.get_active_artists()
            logger.info("Notion 連線正常，可排課藝人 %d 位：%s",
                        len(artists), "、".join(a.name for a in artists) or "（無）")
            await audit.log_event(
                event_name="Bot 啟動",
                action_type="同步", operator="System", source="System",
                after=f"讀到 {len(artists)} 位可排課藝人",
            )
        except Exception:
            logger.exception(
                "Notion 連線失敗！請確認 NOTION_TOKEN 正確，且 5 張 P0 資料庫都已 share 給 integration。"
            )

    async def close(self):
        # 逐步關閉、互不擋路：其中一步失敗也要繼續往下關，避免卡在半關閉狀態。
        if self.teacher_web_runner is not None:
            try:
                await self.teacher_web_runner.cleanup()
            except Exception:
                logger.exception("老師確認 web 服務關閉失敗")
        try:
            await notion_api.close_client()
        except Exception:
            logger.exception("Notion 連線關閉失敗")
        await super().close()


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("缺少 DISCORD_TOKEN，請先在 .env 填入 Discord bot token。")
    if not config.NOTION_TOKEN:
        raise SystemExit("缺少 NOTION_TOKEN，請先在 .env 填入 Notion integration token。")
    bot = KariKariBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
