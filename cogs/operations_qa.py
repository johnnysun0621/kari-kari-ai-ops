"""控方限定的 Notion 營運 AI 查詢入口。"""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs import channel_guard
from services import operations_qa, repositories
from services.permissions import is_admin

logger = logging.getLogger(__name__)


class OperationsQACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="營運查詢", description="管理端：查詢 Notion 任務、專案、會議決策與審核回饋")
    @app_commands.describe(問題="例如：本週有哪些任務到期？上次會議最後決定什麼？")
    @app_commands.default_permissions(administrator=True)
    @is_admin()
    async def operations_query(self, interaction: discord.Interaction, 問題: str):
        if not await channel_guard.ensure_admin_channel(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            tasks, projects, meetings, submissions = await asyncio.gather(
                repositories.get_all_tasks(),
                repositories.get_all_projects(),
                repositories.get_course_meeting_summaries(),
                repositories.get_all_submissions(),
            )
            answer = await operations_qa.answer_operations_question(
                問題, tasks, projects, meetings, submissions
            )
        except operations_qa.OperationsQAError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)
            return
        except Exception:
            logger.exception("營運 AI 查詢失敗")
            await interaction.followup.send("❌ 營運查詢失敗，請稍後再試。", ephemeral=True)
            return
        chunks = [answer[index:index + 1900] for index in range(0, len(answer), 1900)] or [""]
        await interaction.followup.send(f"🤖 **營運查詢**\n\n{chunks[0]}", ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OperationsQACog(bot))
