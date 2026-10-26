"""每一個給人看的介面文字都要寫進營運版或藝人版 SOP（2026-10-26）。

從 cogs/ 抓出按鈕名稱（label）、選單提示（placeholder）、Modal 標題（title）與 slash command 名稱，
逐一確認出現在 docs/sop/ops/ 或 docs/sop/artist/ 的某一頁。新增按鈕卻沒寫 SOP，這裡會失敗。
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_sop", ROOT / "scripts" / "build_sop.py")
build_sop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_sop)

LABEL = re.compile(r'\b(label|placeholder|title)\s*=\s*"([^"{}\n]{2,60})"')
COMMAND = re.compile(r'@app_commands\.command\(\s*name="([^"]+)"')

# 沒有入口的程式碼（見 docs/sop/eng/09-已知限制.md）、早報關閉時才出現的舊式按鈕、
# 太通用的導覽字（上一頁、取消…）與輸入範例，不要求寫進 SOP。
EXEMPT = {
    # 沒有入口：PoolAvailabilityModal、AvailabilityConflictConfirmView、AvailabilityCleanupPreviewView
    "日期（例：8/5 或 2026-08-05）", "開始時間（例：14:00）", "結束時間（例：18:00，可跨日）",
    "備註（選填：限制、偏好或不方便事項）", "再填一筆", "完成", "確認調整後送出",
    "套用全部整理", "\\n**會合併更新**", "\\n**建議封存**",
    # 早報關閉時才有的逐則提醒按鈕
    "收到通知",
    # 通用導覽字、空選項
    "上一頁", "下一頁", "取消", "返回", "關閉", "（沒有任務）",
    # Modal 欄位：SOP 寫的是用途，不逐字列欄位名稱
    "開始時間（例：13:30）", "結束時間（例：17:00）", "開始時間", "任務名稱", "課程名稱",
    "請輸入問題、補充或需要營運處理的事項",
    # Discord 訊息本身的標題（不是要按的東西）
    "📌 營運布告欄",
}


def _is_example(text: str) -> bool:
    return text.startswith(("例：", "例如：", "https://")) or bool(re.fullmatch(r"[\d:/ ]+", text))


def _ui_texts() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted((ROOT / "cogs").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in LABEL.finditer(source):
            text = match.group(2).strip()
            if text not in EXEMPT and not _is_example(text):
                found.setdefault(text, f"{path.name}:{source[:match.start()].count(chr(10)) + 1}")
        for match in COMMAND.finditer(source):
            found.setdefault(f"/{match.group(1)}", f"{path.name}（指令）")
    return found


def _sop_text() -> str:
    return "\n".join(page.body for audience in ("ops", "artist") for page in build_sop.load(audience))


def _strip_emoji(text: str) -> str:
    return re.sub(r"^[^\w（「/]+", "", text).strip()


def test_每個按鈕選單指令都寫進SOP():
    sop = _sop_text()
    missing = [
        f"{text}（{where}）" for text, where in _ui_texts().items()
        if text not in sop and _strip_emoji(text) not in sop
    ]
    assert not missing, "這些介面文字還沒寫進營運版或藝人版 SOP：\n" + "\n".join(missing)


def test_抓得到介面文字():
    texts = _ui_texts()
    assert len(texts) > 80 and "/審核提交" in texts and "我已上傳到 Drive" in texts
