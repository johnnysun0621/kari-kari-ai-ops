"""KariKari AI Ops Bot 進入點：P0-1 排課 MVP。"""
import logging

import discord
from discord.ext import commands

import config
from cogs.scheduling_artist import FillAvailabilityButton
from services import audit, notion_api, repositories

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("karikari")

EXTENSIONS = [
    "cogs.scheduling_admin",
    "cogs.scheduling_artist",
    "cogs.reminders",
]


class KariKariBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        # persistent 按鈕：bot 重啟後公告上的「填寫可用時間」仍可用
        self.add_dynamic_items(FillAvailabilityButton)
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
            logger.info("Notion 連線正常，Active 藝人 %d 位：%s",
                        len(artists), "、".join(a.name for a in artists) or "（無）")
            await audit.log_event(
                event_name="Bot 啟動",
                action_type="同步", operator="System", source="System",
                after=f"讀到 {len(artists)} 位 Active 藝人",
            )
        except Exception:
            logger.exception(
                "Notion 連線失敗！請確認 NOTION_TOKEN 正確，且 5 張 P0 資料庫都已 share 給 integration。"
            )

    async def close(self):
        await notion_api.close_client()
        await super().close()


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("缺少 DISCORD_TOKEN，請先複製 .env.example 為 .env 並填入設定。")
    if not config.NOTION_TOKEN:
        raise SystemExit("缺少 NOTION_TOKEN，請先在 .env 填入 Notion integration token。")
    bot = KariKariBot()
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
