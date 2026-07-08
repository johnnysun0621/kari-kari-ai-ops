"""集中設定：讀取 .env、Notion Data Source ID、時區與 SLA 常數。"""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int = 0) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# === Discord ===
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = _env_int("GUILD_ID")
SCHEDULE_CHANNEL_ID = _env_int("SCHEDULE_CHANNEL_ID")
ADMIN_ROLE_ID = _env_int("ADMIN_ROLE_ID")
ADMIN_NOTIFY_CHANNEL_ID = _env_int("ADMIN_NOTIFY_CHANNEL_ID", SCHEDULE_CHANNEL_ID)

# === Notion ===
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_VERSION = "2025-09-03"

# 正式資料層：KariKari 營運中心（2026-07-07 拍板，11 張資料庫）
# https://app.notion.com/p/38e5806f097b812aba6dc668ebb5e541
DS_ARTISTS = os.getenv("NOTION_DS_ARTISTS", "c0253aec-d18c-47de-a92f-e4a6f9eda641")
DS_SCHEDULE_REQUESTS = os.getenv("NOTION_DS_SCHEDULE_REQUESTS", "028529f8-42ea-41f2-ba81-68d49c438f43")
DS_AVAILABILITY_SLOTS = os.getenv("NOTION_DS_AVAILABILITY_SLOTS", "3726792b-a0f2-47f0-9fb2-2a49a0444266")
DS_COURSE_SESSIONS = os.getenv("NOTION_DS_COURSE_SESSIONS", "12402cad-242d-4caf-978f-9f56b794e646")
DS_AUDIT_LOG = os.getenv("NOTION_DS_AUDIT_LOG", "af855535-af5a-42b7-922a-4bc96449b267")
# P0-2/P0-3 會用到（尚未接）
DS_TASKS = os.getenv("NOTION_DS_TASKS", "f5fdde00-d0b9-4b9b-8786-06432dbdc0c9")
DS_PROJECTS = os.getenv("NOTION_DS_PROJECTS", "579092eb-68ae-4f1c-99c5-253d801ee07d")
DS_PEOPLE = os.getenv("NOTION_DS_PEOPLE", "52413671-535d-40de-90fc-2df3c2c61f93")
DS_SUBMISSIONS = os.getenv("NOTION_DS_SUBMISSIONS", "370b01ba-64a7-4a1e-96de-e7cb83695d58")

# === 業務常數 ===
TZ = ZoneInfo("Asia/Taipei")

# 課程類型（與 Notion Schedule Requests / Course Sessions 的 select 選項一致）
COURSE_TYPES = [
    "歌唱課", "編曲課", "樂理課", "表演藝術課",
    "舞蹈課", "音樂製作課", "企劃會議", "錄音 / 拍攝",
]

# SLA：排課需求建立後 48 小時內要完成發布通告，最多 72 小時
SLA_HOURS = 48
SLA_HARD_LIMIT_HOURS = 72
# 填寫截止前幾小時開始催收
REMIND_BEFORE_DEADLINE_HOURS = 6
# 背景提醒 loop 間隔（分鐘）
REMINDER_INTERVAL_MINUTES = 30

# 預設填寫截止時間（小時，從建立起算）
DEFAULT_FILL_DEADLINE_HOURS = 24

# 候選時段在 Notion「候選時段」text 欄位中的格式：每行 "YYYY-MM-DD HH:MM~HH:MM"
CANDIDATE_LINE_FORMAT = "%Y-%m-%d %H:%M"
