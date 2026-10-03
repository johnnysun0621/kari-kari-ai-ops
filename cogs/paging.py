"""跨 cog 共用的訊息分頁 View（沿用 channel_guard、ui_text 的共用模組模式）。

Discord 單則訊息有 2000 字上限，長清單過去只能截斷。這個 View 讓清單切成多頁後
可以用「上一頁／下一頁」翻，或用下拉選單直接跳到指定頁；只有一頁時都會停用。
"""
import discord

# Discord 單一下拉最多 25 個選項
PAGE_SELECT_LIMIT = 25


def build_page_window(current: int, total: int, limit: int = PAGE_SELECT_LIMIT) -> list[int]:
    """頁數超過下拉上限時，取目前頁附近的一段，並保留頭尾兩頁當錨點。"""
    if total <= limit:
        return list(range(total))
    half = (limit - 2) // 2
    start = max(0, min(current - half, total - (limit - 2)))
    window = list(range(start, start + limit - 2))
    return sorted({0, *window, total - 1})


class PagedTextView(discord.ui.View):
    """純文字清單的翻頁介面；子類別可再加自己的操作按鈕（放 row 0）。

    版面配置：row 0 留給子類別、row 1 上一頁／下一頁、row 2 跳頁下拉。
    """

    def __init__(
        self,
        pages: list[str],
        operator_id: int,
        *,
        timeout: float | None = 600,
        page_labels: list[str] | None = None,
    ):
        super().__init__(timeout=timeout)
        self.pages = pages or [""]
        self.operator_id = operator_id
        # 每頁的說明文字（例如日期範圍）；使用者找的通常是內容而不是頁碼。
        self.page_labels = page_labels or []
        self.page = 0
        self.page_select: discord.ui.Select | None = None
        if self.page_count > 1:
            self.page_select = discord.ui.Select(row=2)
            self.page_select.callback = self._on_page_selected
            self.add_item(self.page_select)
        self._sync_page_controls()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def current_text(self) -> str:
        return self.pages[self.page]

    def _page_option_label(self, index: int) -> str:
        return f"第 {index + 1}/{self.page_count} 頁"

    def _page_option_description(self, index: int) -> str | None:
        if index < len(self.page_labels) and self.page_labels[index]:
            return self.page_labels[index][:100]
        return None

    def _sync_page_controls(self) -> None:
        single_page = self.page_count <= 1
        self.previous_page.disabled = single_page or self.page == 0
        self.next_page.disabled = single_page or self.page >= self.page_count - 1
        if self.page_select is None:
            return
        self.page_select.placeholder = f"跳到指定頁（目前第 {self.page + 1}/{self.page_count} 頁）…"
        self.page_select.options = [
            discord.SelectOption(
                label=self._page_option_label(index),
                value=str(index),
                description=self._page_option_description(index),
                default=index == self.page,
            )
            for index in build_page_window(self.page, self.page_count)
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.operator_id:
            return True
        await interaction.response.send_message(
            "❌ 這個清單只能由原本執行指令的人操作。", ephemeral=True
        )
        return False

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        self.page = max(0, min(page, self.page_count - 1))
        self._sync_page_controls()
        await interaction.response.edit_message(content=self.current_text(), view=self)

    async def _on_page_selected(self, interaction: discord.Interaction) -> None:
        assert self.page_select is not None
        await self._change_page(interaction, int(self.page_select.values[0]))

    @discord.ui.button(label="上一頁", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label="下一頁", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._change_page(interaction, self.page + 1)
