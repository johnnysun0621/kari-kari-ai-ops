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


def _env_weekday(name: str, default: int) -> int:
    """0＝週一 … 6＝週日；`daily`／`每天` 回 -1（每天都發）。無法解析就用預設。"""
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"daily", "每天"}:
        return -1
    try:
        weekday = int(value)
    except ValueError:
        return default
    return weekday if 0 <= weekday <= 6 else default


def _env_tuple(name: str, default: str) -> tuple[str, ...]:
    """逗號分隔的 .env 清單轉 tuple；值必須是 Notion 既有選項字串，一字不對就永遠不命中。"""
    return tuple(
        value.strip() for value in os.getenv(name, default).split(",") if value.strip()
    )


@dataclass(frozen=True)
class TenantConfig:
    """一間公司（租戶）的完整設定：Discord、Notion 資料層、業務常數。"""

    name: str

    # === Discord ===
    discord_token: str
    discord_client_id: str
    discord_client_secret: str
    discord_oauth_redirect_uri: str
    guild_id: int
    schedule_channel_id: int
    admin_role_id: int
    admin_notify_channel_id: int
    # 控方 slash command 頻道；未設定時沿用營運通知頻道
    admin_command_channel_id: int
    task_channel_id: int
    # 公告區（唯讀建議）：公開類型 Task 與收集/排課公告；未設定時 fallback 排課頻道
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
    # 這些 Task 類型除了私訊藝人，也會發到公告頻道。
    public_task_types: tuple[str, ...]
    # 無條件硬擋藝人可配合時段的任務類型（M1-05）。新增類型想套用這個既有行為，
    # 改這裡就好，不用改程式碼——跟 public_task_types 同一個模式。
    hard_block_task_types: tuple[str, ...]
    # 不判逾期的專案類型（M5-03）。同上，新增類型想套用「不判逾期」就改這裡。
    no_overdue_project_types: tuple[str, ...]
    # SLA：排課需求建立後 N 小時內要完成發布通告
    sla_hours: int
    sla_hard_limit_hours: int
    # 填寫截止前幾小時開始催收
    remind_before_deadline_hours: int
    # Tasks 沒有勾任何「提醒規則」時套用的預設規則；留空＝不提醒（維持舊行為）
    default_reminder_rules: tuple[str, ...]
    # 「提前交件」任務沒填「提交期限」時，交件死線＝截止日往前推幾天（會議 2026-09-12 定 T-5）
    approval_lead_days: int
    # 藝人未按「收到任務」時的重送間隔（小時）
    task_ack_reminder_hours: float
    # 提供 Drive 資料夾後，等待藝人上傳的有效期（分鐘）
    file_upload_wait_minutes: int
    # 背景提醒 loop 間隔（分鐘）
    reminder_interval_minutes: float
    # 待審核提交的持續提醒間隔（小時）
    review_reminder_interval_hours: float
    # 待審核提交首次催辦門檻（小時）
    review_reminder_after_hours: float
    # 預設填寫截止時間（小時，從建立起算）
    default_fill_deadline_hours: int
    # Notion 布告欄背景同步間隔（分鐘）
    bulletin_sync_interval_minutes: int = 5
    # 發文類停在「已完成」的待確認發布彙整（M2-05）：星期幾發（0＝週一…6＝週日，-1＝每天）、
    # 幾點發、截止日過幾天才列入
    publish_confirm_digest_weekday: int = 0
    publish_confirm_digest_hour: int = 10
    publish_confirm_digest_after_days: int = 3
    # 每日催辦（M3-11）：已逾期／需修改催藝人、待審核催審核人員，每天這個整點跑一次
    daily_nag_hour: int = 10
    # 課後提醒老師（M5-01）：課程結束後幾小時發第一次、最多每天催幾天（之後改通知控方）
    teacher_reminder_after_hours: float = 2
    teacher_reminder_max_days: int = 7

    # === Google Drive（作業歸檔）===
    # 總資料夾 ID（底下放各藝人專區）；空字串＝Drive 歸檔未啟用
    drive_root_folder_id: str = ""

    # === 會議秘書小律（可選，未設定 LLM_API_KEY＝功能停用）===
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-luna"
    llm_max_tokens: int = 800
    llm_daily_token_limit: int = 0
    llm_per_artist_daily_token_limit: int = 0
    # 排除清單：這些「會議類型」永遠不會被小律讀取，即使藝人可見=true。
    # 課程／課程會議已改用其他管道處理；公司例會、管理層會議是內部限定，不開放討論空間。
    # 其餘類型（含客戶之後在 Notion 新增的任何新類型）預設開放，只靠「藝人可見」逐筆把關，
    # 不需要客戶為了新增會議類型另外維護這份清單。
    qa_hidden_meeting_types: tuple[str, ...] = ("課程", "課程會議", "公司例會", "管理層會議")

    # === 本機 web 服務（只服務 Discord OAuth 回呼，不需要對外網域）===
    web_port: int = 8787

    @property
    def resolved_announce_channel_id(self) -> int:
        """公告發送目標：公告區，未設定 fallback 排課頻道（單一來源）。"""
        return self.announce_channel_id or self.schedule_channel_id


def _load_karikari_from_env() -> TenantConfig:
    """KariKari 租戶：正式資料層（2026-07-07 拍板，11 張資料庫）。

    九組 Data Source ID 全部必填、沒有預設值，見 .env.example。
    資料層架構與欄位對照見 docs/foundation/NOTION_SCHEMA.md
    （Notion 網址含客戶頁面 ID，不寫在這個 public repo 裡）。
    """
    schedule_channel_id = _env_int("SCHEDULE_CHANNEL_ID")
    admin_notify_channel_id = _env_int("ADMIN_NOTIFY_CHANNEL_ID", schedule_channel_id)
    return TenantConfig(
        name="LURASUPA",
        discord_token=os.getenv("DISCORD_TOKEN", ""),
        discord_client_id=os.getenv("DISCORD_CLIENT_ID", ""),
        discord_client_secret=os.getenv("DISCORD_CLIENT_SECRET", ""),
        discord_oauth_redirect_uri=os.getenv("DISCORD_OAUTH_REDIRECT_URI", ""),
        guild_id=_env_int("GUILD_ID"),
        schedule_channel_id=schedule_channel_id,
        admin_role_id=_env_int("ADMIN_ROLE_ID"),
        admin_notify_channel_id=admin_notify_channel_id,
        admin_command_channel_id=_env_int(
            "ADMIN_COMMAND_CHANNEL_ID",
            admin_notify_channel_id,
        ),
        task_channel_id=_env_int("TASK_CHANNEL_ID"),
        announce_channel_id=_env_int("ANNOUNCE_CHANNEL_ID"),
        bulletin_channel_id=_env_int("BULLETIN_CHANNEL_ID"),
        ai_channel_id=_env_int("AI_CHANNEL_ID"),
        notion_token=os.getenv("NOTION_TOKEN", ""),
        notion_version="2025-09-03",
        # 鐵則 1：公司特定值一律從 .env 注入，不留寫死的 fallback。
        # 這裡曾經放九組寫死的真實 ID；因為 load_dotenv() 必定覆蓋，那些值永遠執行不到，
        # 卻把正式環境的資料庫 ID 公開在 repo 裡，而且與 .env 實際值已經不一致——
        # 真正的風險是 .env 漏載時 bot 會默默連到錯誤的九個資料庫而不是大聲失敗。
        # 缺欄位時留空，由 bot.py 啟動檢查擋下（見 _missing_data_source_env）。
        ds_artists=os.getenv("NOTION_DS_ARTISTS", ""),
        ds_availability_slots=os.getenv("NOTION_DS_AVAILABILITY_SLOTS", ""),
        ds_audit_log=os.getenv("NOTION_DS_AUDIT_LOG", ""),
        ds_tasks=os.getenv("NOTION_DS_TASKS", ""),
        ds_projects=os.getenv("NOTION_DS_PROJECTS", ""),
        ds_people=os.getenv("NOTION_DS_PEOPLE", ""),
        ds_submissions=os.getenv("NOTION_DS_SUBMISSIONS", ""),
        ds_bulletin=os.getenv("NOTION_DS_BULLETIN", ""),
        ds_course_meetings=os.getenv("NOTION_DS_COURSE_MEETINGS", ""),
        tz=ZoneInfo("Asia/Taipei"),
        public_task_types=tuple(
            value.strip()
            # 值必須是 Notion「任務類型」既有選項；「拍攝」不是，寫了也永遠不命中
            for value in os.getenv("PUBLIC_TASK_TYPES", "通告,演出").split(",")
            if value.strip()
        ),
        hard_block_task_types=_env_tuple("HARD_BLOCK_TASK_TYPES", "錄音,演出,會議溝通"),
        no_overdue_project_types=_env_tuple("NO_OVERDUE_PROJECT_TYPES", "課程培訓"),
        sla_hours=48,
        sla_hard_limit_hours=72,
        remind_before_deadline_hours=_env_float("REMIND_BEFORE_DEADLINE_HOURS", 6),
        task_ack_reminder_hours=_env_float("TASK_ACK_REMINDER_HOURS", 12),
        file_upload_wait_minutes=_env_int("FILE_UPLOAD_WAIT_MINUTES", 180),
        # 2026-09-20 起預設只剩「當天」：提前幾天交件由「提前交件」＋「提交期限」決定，
        # 不再靠提醒規則硬塞 5 天。上課等不提前交件的任務因此不會再收到 T-5。
        default_reminder_rules=tuple(
            value.strip()
            # 預設＝D-5 ＋ D-3（09/22 會議「兩個節點並行」）。與任務勾選的規則取聯集，
            # 不是替換 —— 見 repositories.compute_due_reminder_stages()
            for value in os.getenv("DEFAULT_REMINDER_RULES", "5 天前,3 天前").split(",")
            if value.strip()
        ),
        approval_lead_days=_env_int("APPROVAL_LEAD_DAYS", 5),
        reminder_interval_minutes=_env_float("REMINDER_INTERVAL_MINUTES", 30),
        review_reminder_interval_hours=_env_float("REVIEW_REMINDER_INTERVAL_HOURS", 12),
        review_reminder_after_hours=_env_float("REVIEW_REMINDER_AFTER_HOURS", 24),
        default_fill_deadline_hours=24,
        bulletin_sync_interval_minutes=_env_int("BULLETIN_SYNC_INTERVAL_MINUTES", 5),
        publish_confirm_digest_weekday=_env_weekday("PUBLISH_CONFIRM_DIGEST_WEEKDAY", 0),
        publish_confirm_digest_hour=min(max(_env_int("PUBLISH_CONFIRM_DIGEST_HOUR", 10), 0), 23),
        publish_confirm_digest_after_days=max(_env_int("PUBLISH_CONFIRM_DIGEST_AFTER_DAYS", 3), 0),
        daily_nag_hour=min(max(_env_int("DAILY_NAG_HOUR", 10), 0), 23),
        teacher_reminder_after_hours=max(_env_float("TEACHER_REMINDER_AFTER_HOURS", 2), 0),
        teacher_reminder_max_days=max(_env_int("TEACHER_REMINDER_MAX_DAYS", 7), 1),
        drive_root_folder_id=os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_model=os.getenv("LLM_MODEL", "gpt-5.6-luna"),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 800),
        llm_daily_token_limit=_env_int("LLM_DAILY_TOKEN_LIMIT", 0),
        llm_per_artist_daily_token_limit=_env_int("LLM_PER_ARTIST_DAILY_TOKEN_LIMIT", 0),
        qa_hidden_meeting_types=tuple(
            value.strip()
            for value in os.getenv(
                "QA_HIDDEN_MEETING_TYPES", "課程,課程會議,公司例會,管理層會議"
            ).split(",")
            if value.strip()
        ),
        web_port=_env_int("WEB_PORT", 8787),
    )


TENANT = _load_karikari_from_env()


def get_tenant() -> TenantConfig:
    """目前運行中的租戶設定。多租戶時改由 registry 依 guild 解析。"""
    return TENANT


# 租戶欄位 → .env 變數名。Data Source ID 沒有合理預設值：連錯資料庫比連不上更糟，
# 所以缺任何一個就讓 bot 啟動失敗，而不是讓它帶著空字串去打 Notion。
DATA_SOURCE_ENV_VARS = {
    "ds_artists": "NOTION_DS_ARTISTS",
    "ds_availability_slots": "NOTION_DS_AVAILABILITY_SLOTS",
    "ds_audit_log": "NOTION_DS_AUDIT_LOG",
    "ds_tasks": "NOTION_DS_TASKS",
    "ds_projects": "NOTION_DS_PROJECTS",
    "ds_people": "NOTION_DS_PEOPLE",
    "ds_submissions": "NOTION_DS_SUBMISSIONS",
    "ds_bulletin": "NOTION_DS_BULLETIN",
    "ds_course_meetings": "NOTION_DS_COURSE_MEETINGS",
}


def missing_data_source_env(tenant: TenantConfig | None = None) -> list[str]:
    """回傳未設定的 Data Source 環境變數名稱（已排序）。空 list 代表九組都齊。"""
    target = tenant if tenant is not None else get_tenant()
    return sorted(
        env_name
        for field, env_name in DATA_SOURCE_ENV_VARS.items()
        if not str(getattr(target, field, "") or "").strip()
    )


# ======================================================================
# 向後相容別名（既有程式碼使用；新程式碼請改用 get_tenant()）
# ======================================================================

# === Discord ===
DISCORD_TOKEN = TENANT.discord_token
GUILD_ID = TENANT.guild_id
SCHEDULE_CHANNEL_ID = TENANT.schedule_channel_id
ADMIN_ROLE_ID = TENANT.admin_role_id
ADMIN_NOTIFY_CHANNEL_ID = TENANT.admin_notify_channel_id
ADMIN_COMMAND_CHANNEL_ID = TENANT.admin_command_channel_id
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
PUBLIC_TASK_TYPES = list(TENANT.public_task_types)
HARD_BLOCK_TASK_TYPES = set(TENANT.hard_block_task_types)
NO_OVERDUE_PROJECT_TYPES = set(TENANT.no_overdue_project_types)
SLA_HOURS = TENANT.sla_hours
SLA_HARD_LIMIT_HOURS = TENANT.sla_hard_limit_hours
REMIND_BEFORE_DEADLINE_HOURS = TENANT.remind_before_deadline_hours
REMINDER_INTERVAL_MINUTES = TENANT.reminder_interval_minutes
REVIEW_REMINDER_INTERVAL_HOURS = TENANT.review_reminder_interval_hours
DEFAULT_FILL_DEADLINE_HOURS = TENANT.default_fill_deadline_hours
DRIVE_ROOT_FOLDER_ID = TENANT.drive_root_folder_id
BULLETIN_SYNC_INTERVAL_MINUTES = TENANT.bulletin_sync_interval_minutes

# 候選時段在 Notion「候選時段」text 欄位中的格式：每行 "YYYY-MM-DD HH:MM~HH:MM"
# （內部資料格式，跨租戶共用，不屬於 TenantConfig）
CANDIDATE_LINE_FORMAT = "%Y-%m-%d %H:%M"
