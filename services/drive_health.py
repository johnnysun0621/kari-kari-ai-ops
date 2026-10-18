"""每日 Drive 健康檢查（2026-10-18）。

為什麼需要：2026-08-22 OAuth token 失效後，Drive 交件全部失敗，直到 10/18 QA 才發現，
因為 bot 只在有人交件時才碰 Drive，而失敗只被吃成「建立資料夾失敗」。
OAuth app 已發布為正式（沒有 7 天過期），但 refresh token 約 6 個月沒用仍會失效，
所以每天讀一次總資料夾：同時讓授權保持在使用中，壞了當天就通知控方。

狀態（最後檢查日、是否正在失敗）存在 notification_log 的共用 state，重啟後同一天不重跑。
"""
from datetime import datetime

LAST_DATE_KEY = "drive_health_last_date"
FAILING_KEY = "drive_health_failing"


def check_due(now: datetime, last_date: str | None, hour: int) -> bool:
    """過了指定整點、今天還沒檢查過。"""
    return now.hour >= hour and last_date != now.date().isoformat()


def classify_error(exc: BaseException) -> str:
    """把 Google 的錯誤翻成控方看得懂的原因。"""
    text = f"{type(exc).__name__} {exc}"
    if "invalid_grant" in text or "RefreshError" in text:
        return "授權失效"
    if "404" in text or "notFound" in text or "File not found" in text:
        return "找不到總資料夾"
    if "403" in text or "insufficientPermissions" in text or "forbidden" in text.lower():
        return "沒有總資料夾的權限"
    return "Drive 連線失敗"


FIX_STEPS = {
    "授權失效": (
        "1. 在跑 bot 的電腦執行 `.venv\\Scripts\\python.exe scripts\\authorize_drive.py`，"
        "用擁有總資料夾的公司 Google 帳號登入並同意\n"
        "2. 執行 restart_bot.bat 立即生效（沒重啟的話，明天的檢查也會自動換上新授權）"
    ),
    "找不到總資料夾": (
        "總資料夾可能被刪除或移到垃圾桶。到 Drive 的垃圾桶找「藝人提交檔案專用（小律維護，勿動）」還原；"
        "若要換新資料夾，改 .env 的 GOOGLE_DRIVE_ROOT_FOLDER_ID 後重啟 bot"
    ),
    "沒有總資料夾的權限": (
        "授權的公司 Google 帳號看不到總資料夾，可能是資料夾擁有者變了或共用被移除；"
        "請把總資料夾的編輯權限還給那個帳號"
    ),
    "Drive 連線失敗": "可能是 Google 暫時性問題；明天會再檢查一次。連續兩天都失敗請查 logs/bot.log",
}


def failure_message(reason: str, detail: str) -> str:
    return (
        f"🔴 **Google Drive 健康檢查失敗：{reason}**\n"
        "藝人現在交件會失敗（拿不到 Drive 資料夾），老師的大型批改檔也無法歸檔。\n\n"
        f"**怎麼修**\n{FIX_STEPS.get(reason, FIX_STEPS['Drive 連線失敗'])}\n\n"
        f"詳細：`{detail[:300]}`\n"
        "完整說明見 docs/operations/DRIVE_OAUTH_SETUP.md「常見問題」。每天最多通知一次。"
    )


RECOVERED_MESSAGE = "🟢 Google Drive 健康檢查恢復正常，藝人可以照常交件。"
