"""集中設定：讀取 .env、Notion Data Source ID、時區與 SLA 常數。

TenantConfig 是租戶（公司）設定的唯一形狀（PROJECT_PRINCIPLES：平展化）。
KariKari 是第一個租戶，從 .env 載入為 TENANT。
模組層級常數是 TENANT 的向後相容別名——既有程式碼繼續可用；
新程式碼請改吃 get_tenant() 回傳的設定物件，不要再讀模組常數。
"""
import os
from dataclasses import dataclass
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


def _env_float(name: str, default: float = 0.0) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class TenantConfig:
    """一間公司（租戶）的完整設定：Discord、Notion 資料層、業務常數。"""

    name: str

    # === Discord ===
    discord_token: str
    guild_id: int
    schedule_channel_id: int
    admin_role_id: int
    admin_notify_channel_id: int
    task_channel_id: int
    # 公告區（唯讀建議）：正式通告與收集/排課公告；未設定時 fallback 排課頻道
    announce_channel_id: int
    # Notion 營運布告欄同步到 Discord 的頻道；未設定時使用公告區
    bulletin_channel_id: int
    # 課程 AI 助教頻道；未設定時不啟用頻道限制
    ai_channel_id: int

    # === Notion ===
    notion_token: str
    notion_version: str
    ds_artists: str
    ds_availability_slots: str
    ds_audit_log: str
    # P0-2/P0-3 使用
    ds_tasks: str
    ds_projects: str
    ds_people: str
    ds_submissions: str
    ds_bulletin: str
    ds_course_meetings: str

    # === 業務常數 ===
    tz: ZoneInfo
    course_types: tuple[str, ...]
    # SLA：排課需求建立後 N 小時內要完成發布通告
    sla_hours: int
    sla_hard_limit_hours: int
    # 填寫截止前幾小時開始催收
    remind_before_deadline_hours: int
    # 背景提醒 loop 間隔（分鐘）
    reminder_interval_minutes: float
    # 預設填寫截止時間（小時，從建立起算）
    default_fill_deadline_hours: int
    # Notion 布告欄背景同步間隔（分鐘）
    bulletin_sync_interval_minutes: int = 5

    # === Google Drive（作業歸檔）===
    # 總資料夾 ID（底下放各藝人專區）；空字串＝Drive 歸檔未啟用
    drive_root_folder_id: str = ""

    # === 課程 AI 助教（可選，未設定＝功能停用）===
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_max_tokens: int = 800
    llm_daily_token_limit: int = 50000

    # === 老師外部確認連結（Phase 2）===
    # secret 未設定＝功能停用；base_url＝Tunnel 對外網址（開發期用 trycloudflare）
    teacher_link_secret: str = ""
    teacher_link_base_url: str = ""
    web_port: int = 8787

    @property
    def resolved_announce_channel_id(self) -> int:
        """公告發送目標：公告區，未設定 fallback 排課頻道（單一來源）。"""
        return self.announce_channel_id or self.schedule_channel_id


def _load_karikari_from_env() -> TenantConfig:
    """KariKari 租戶：正式資料層（2026-07-07 拍板，11 張資料庫）。

    https://app.notion.com/p/38e5806f097b812aba6dc668ebb5e541
    Data Source ID 內建預設值；Notion 端重建資料庫時才需要在 .env 覆寫。
    """
    schedule_channel_id = _env_int("SCHEDULE_CHANNEL_ID")
    return TenantConfig(
        name="KariKari",
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        guild_id=_env_int("GUILD_ID"),
        schedule_channel_id=schedule_channel_id,
        admin_role_id=_env_int("ADMIN_ROLE_ID"),
        admin_notify_channel_id=_env_int("ADMIN_NOTIFY_CHANNEL_ID", schedule_channel_id),
        task_channel_id=_env_int("TASK_CHANNEL_ID"),
        announce_channel_id=_env_int("ANNOUNCE_CHANNEL_ID"),
        bulletin_channel_id=_env_int("BULLETIN_CHANNEL_ID"),
        ai_channel_id=_env_int("AI_CHANNEL_ID"),
        notion_token=os.getenv("NOTION_TOKEN", ""),
        notion_version="2025-09-03",
        ds_artists=os.getenv("NOTION_DS_ARTISTS", "c0253aec-d18c-47de-a92f-e4a6f9eda641"),
        ds_availability_slots=os.getenv("NOTION_DS_AVAILABILITY_SLOTS", "3726792b-a0f2-47f0-9fb2-2a49a0444266"),
        ds_audit_log=os.getenv("NOTION_DS_AUDIT_LOG", "af855535-af5a-42b7-922a-4bc96449b267"),
        ds_tasks=os.getenv("NOTION_DS_TASKS", "f5fdde00-d0b9-4b9b-8786-06432dbdc0c9"),
        ds_projects=os.getenv("NOTION_DS_PROJECTS", "579092eb-68ae-4f1c-99c5-253d801ee07d"),
        ds_people=os.getenv("NOTION_DS_PEOPLE", "52413671-535d-40de-90fc-2df3c2c61f93"),
        ds_submissions=os.getenv("NOTION_DS_SUBMISSIONS", "370b01ba-64a7-4a1e-96de-e7cb83695d58"),
        ds_bulletin=os.getenv("NOTION_DS_BULLETIN", "d605806f-097b-823e-ac69-07fa536ba05f"),
        ds_course_meetings=os.getenv("NOTION_DS_COURSE_MEETINGS", "0a95806f-097b-8368-bb02-8705bdfa744b"),
        tz=ZoneInfo("Asia/Taipei"),
        # 課程類型（與 Tasks.課程類別 select 選項一致）
        course_types=(
            "歌唱課", "編曲課", "樂理課", "表演藝術課",
            "舞蹈課", "音樂製作課", "聽力訓練課",
        ),
        sla_hours=48,
        sla_hard_limit_hours=72,
        remind_before_deadline_hours=6,
        reminder_interval_minutes=_env_float("REMINDER_INTERVAL_MINUTES", 30),
        default_fill_deadline_hours=24,
        bulletin_sync_interval_minutes=_env_int("BULLETIN_SYNC_INTERVAL_MINUTES", 5),
        drive_root_folder_id=os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 800),
        llm_daily_token_limit=_env_int("LLM_DAILY_TOKEN_LIMIT", 50000),
        teacher_link_secret=os.getenv("TEACHER_LINK_SECRET", ""),
        teacher_link_base_url=os.getenv("TEACHER_LINK_BASE_URL", ""),
        web_port=_env_int("WEB_PORT", 8787),
    )


TENANT = _load_karikari_from_env()


def get_tenant() -> TenantConfig:
    """目前運行中的租戶設定。多租戶時改由 registry 依 guild 解析。"""
    return TENANT


# ======================================================================
# 向後相容別名（既有程式碼使用；新程式碼請改用 get_tenant()）
# ======================================================================

# === Discord ===
DISCORD_TOKEN = TENANT.discord_token
GUILD_ID = TENANT.guild_id
SCHEDULE_CHANNEL_ID = TENANT.schedule_channel_id
ADMIN_ROLE_ID = TENANT.admin_role_id
ADMIN_NOTIFY_CHANNEL_ID = TENANT.admin_notify_channel_id
TASK_CHANNEL_ID = TENANT.task_channel_id
ANNOUNCE_CHANNEL_ID = TENANT.announce_channel_id
BULLETIN_CHANNEL_ID = TENANT.bulletin_channel_id
AI_CHANNEL_ID = TENANT.ai_channel_id

# === Notion ===
NOTION_TOKEN = TENANT.notion_token
NOTION_VERSION = TENANT.notion_version
DS_ARTISTS = TENANT.ds_artists
DS_AVAILABILITY_SLOTS = TENANT.ds_availability_slots
DS_AUDIT_LOG = TENANT.ds_audit_log
DS_TASKS = TENANT.ds_tasks
DS_PROJECTS = TENANT.ds_projects
DS_PEOPLE = TENANT.ds_people
DS_SUBMISSIONS = TENANT.ds_submissions
DS_BULLETIN = TENANT.ds_bulletin
DS_COURSE_MEETINGS = TENANT.ds_course_meetings

# === 業務常數 ===
TZ = TENANT.tz
COURSE_TYPES = list(TENANT.course_types)
SLA_HOURS = TENANT.sla_hours
SLA_HARD_LIMIT_HOURS = TENANT.sla_hard_limit_hours
REMIND_BEFORE_DEADLINE_HOURS = TENANT.remind_before_deadline_hours
REMINDER_INTERVAL_MINUTES = TENANT.reminder_interval_minutes
DEFAULT_FILL_DEADLINE_HOURS = TENANT.default_fill_deadline_hours
DRIVE_ROOT_FOLDER_ID = TENANT.drive_root_folder_id
BULLETIN_SYNC_INTERVAL_MINUTES = TENANT.bulletin_sync_interval_minutes

# 候選時段在 Notion「候選時段」text 欄位中的格式：每行 "YYYY-MM-DD HH:MM~HH:MM"
# （內部資料格式，跨租戶共用，不屬於 TenantConfig）
CANDIDATE_LINE_FORMAT = "%Y-%m-%d %H:%M"
