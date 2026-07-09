"""身分與權限判斷——Phase 3 權限層的起點。

所有「誰能做什麼」的判斷集中在這裡：cog 不自己寫權限邏輯，
之後接 Members.身份類型／可見範圍、Submissions.權限等級也在這一層擴充。
"""
import discord
from discord import app_commands

import config


def is_admin():
    """管理指令 check：Discord Administrator 或租戶設定的控方身分組。"""
    def predicate(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is not None and perms.administrator:
            return True
        admin_role_id = config.get_tenant().admin_role_id
        return any(r.id == admin_role_id for r in getattr(interaction.user, "roles", []))
    return app_commands.check(predicate)
