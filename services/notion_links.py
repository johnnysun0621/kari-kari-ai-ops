"""藝人端零 Notion 入口：偵測與清除輸出裡的 Notion 網址。

為什麼整條規則存在：Notion 的逐頁權限管不到頁面上方的導覽列。藝人只要拿到
任何一個頁面網址，就能往上翻到該頁所屬的資料庫——從一筆任務翻到整個 Tasks
（所有藝人的任務），從一筆會議紀錄翻到整個會議資料庫。所以「這一頁藝人可以看」
不等於「這一頁的網址可以給藝人」，藝人看得到的輸出一律不帶 Notion 連結。

網址也不能進 LLM 的 context：模型會把它抄進回答，等於繞過所有輸出端的處理。
"""
import re

# notion.so／notion.site／app.notion.com 都要涵蓋，含沒有 scheme 的寫法。
_HOST = r"(?:[\w-]+\.)*notion\.(?:so|site|com)"
_MARKDOWN_LINK = re.compile(
    rf"\[([^\]]*)\]\(\s*<?(?:https?://)?{_HOST}/[^\s)]*>?\s*\)",
    re.IGNORECASE,
)
_BARE_URL = re.compile(
    rf"<?(?:https?://)?{_HOST}/[^\s>)\]]*>?",
    re.IGNORECASE,
)


def contains_notion_link(text: str) -> bool:
    """給測試與防呆用：這段文字是否還帶著 Notion 入口。"""
    return bool(text) and bool(_BARE_URL.search(text))


def strip_notion_links(text: str) -> str:
    """把 Notion 連結拿掉，markdown 連結只留下顯示文字。

    只清 Notion 網域。會議或任務內容裡提到的其他連結（參考影片、共用雲端、
    Drive 資料夾）是藝人該拿到的東西，全域砍網址會誤傷。
    """
    if not text:
        return text
    cleaned = _MARKDOWN_LINK.sub(r"\1", text)
    cleaned = _BARE_URL.sub("", cleaned)
    # 拿掉網址後常留下「來源： 」這種懸空空白，收一下再回傳。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines())
