"""每小時清一批超過保留天數的 Audit Log（M7-09）：存成本機 CSV 後移到 Notion 垃圾桶。

自己一個 loop，不排在提醒 loop 裡：第一次上線要清兩千多筆，分好幾輪跑，不能卡住早報和派發。
"""
import logging

from discord.ext import commands, tasks

from services import audit_retention

logger = logging.getLogger(__name__)


class AuditRetentionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.retention_loop.start()

    def cog_unload(self):
        self.retention_loop.cancel()

    @tasks.loop(hours=1)
    async def retention_loop(self):
        try:
            result = await audit_retention.run_once()
        except Exception:
            logger.exception("Audit Log 舊紀錄清理失敗")
            return
        if result.found:
            logger.info(
                "Audit Log 舊紀錄：找到 %s 筆，存進 CSV %s 筆，移到垃圾桶 %s 筆，失敗 %s 筆",
                result.found, result.archived, result.trashed, result.failed,
            )

    @retention_loop.before_loop
    async def before_retention(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AuditRetentionCog(bot))
