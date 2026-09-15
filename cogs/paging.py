"""跨 cog 共用的訊息分頁 View（沿用 channel_guard、ui_text 的共用模組模式）。

Discord 單則訊息有 2000 字上限，長清單過去只能截斷。這個 View 讓清單切成多頁後
可以用「上一頁／下一頁」翻完全部內容；只有一頁時翻頁按鈕會停用。
"""
import discord


class PagedTextView(discord.ui.View):
    """純文字清單的翻頁介面；子類別可再加自己的操作按鈕（放 row 0）。"""

    def __init__(
        self,
        pages: list[str],
        operator_id: int,
        *,
        timeout: float | None = 600,
    ):
        super().__init__(timeout=timeout)
        self.pages = pages or [""]
        self.operator_id = operator_id
        self.page = 0
        self._sync_page_buttons()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def current_text(self) -> str:
        return self.pages[self.page]

    def _sync_page_buttons(self) -> None:
        single_page = self.page_count <= 1
        self.previous_page.disabled = single_page or self.page == 0
        self.next_page.disabled = single_page or self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message(
            "❌ 這個清單只能由原本執行指令的人操作。", ephemeral=True
        )
        return False

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        self.page = max(0, min(page, self.page_count - 1))
        self._sync_page_buttons()
        await interaction.response.edit_message(content=self.current_text(), view=self)

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_page(interaction, self.page + 1)
