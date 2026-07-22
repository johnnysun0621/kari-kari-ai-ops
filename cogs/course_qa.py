"""Discord 課程 AI 助教。"""
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs import channel_guard
from services import course_qa, repositories

logger = logging.getLogger(__name__)
_COOLDOWN_SECONDS = 30


class CourseQACog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_question_at: dict[int, float] = {}

    @app_commands.command(name="課程問答", description="查詢自己已確認且可見的課程會議紀錄")
    @app_commands.describe(問題="例如：上次歌唱課老師交代我練習什麼？")
    async def course_question(self, interaction: discord.Interaction, 問題: str):
        if not await channel_guard.ensure_ai_channel(interaction):
            return
        now = time.monotonic()
        remaining = _COOLDOWN_SECONDS - (now - self._last_question_at.get(interaction.user.id, 0))
        if remaining > 0:
            await interaction.response.send_message(
                f"請 {remaining:.0f} 秒後再提問。", ephemeral=True
            )
            return
        self._last_question_at[interaction.user.id] = now
        await interaction.response.defer(ephemeral=True, thinking=True)
        artist = await repositories.find_artist_by_discord_id(interaction.user.id)
        if artist is None:
            await interaction.followup.send("❌ 找不到你的藝人資料，請聯絡控方。", ephemeral=True)
            return
        try:
            records = await repositories.get_course_meetings_for_artist(artist.page_id)
        except Exception:
            logger.exception("讀取藝人課程紀錄失敗")
            await interaction.followup.send("❌ 讀取課程紀錄失敗，請稍後再試。", ephemeral=True)
            return
        if not records:
            await interaction.followup.send(
                "目前沒有可供你查詢的課程紀錄。請確認控方已勾選 Notion 的「藝人可見」。",
                ephemeral=True,
            )
            return
        try:
            answer = await course_qa.answer_course_question(artist.name, 問題, records)
        except course_qa.CourseQAError as exc:
            logger.warning("課程助教未能回答：%s", exc)
            await interaction.followup.send(
                "⚠️ 課程助教目前尚未完成 API 設定，請聯絡控方。", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"🤖 **課程助教**\n\n{answer}", ephemeral=True
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        logger.exception("課程問答指令失敗", exc_info=error)
        message = "❌ 課程問答執行失敗，請稍後再試。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CourseQACog(bot))
