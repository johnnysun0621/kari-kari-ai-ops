"""啟動時比對：程式拿來篩 Notion 的選項文字，Notion 上還在不在（2026-10-18）。

Notion 的 select filter 只要帶到不存在的選項，整個查詢就回 400。客戶在 Notion 刪掉或改名一個
選項，對應功能就整個停擺，而且畫面上只會看到「讀取失敗」。2026-10-18 記錄檔上線第一天就抓到
小律因為「公司例會」「管理層會議」已被刪除而每次查詢失敗。

這裡列的是「程式寫死、會拿去比對或篩選」的選項。新增這類比對時把它加進 checks()。
`options` 為空的項目只檢查欄位存在（例如 checkbox 篩選：欄位被刪掉一樣整個查詢 400）。
"""
from dataclasses import dataclass

import config
from services import notion_api, ops_notice, repositories


@dataclass(frozen=True)
class OptionCheck:
    data_source: str
    property_name: str
    options: tuple[str, ...]
    impact: str


def checks() -> list[OptionCheck]:
    tenant = config.get_tenant()
    return [
        OptionCheck(
            config.DS_TASKS, "任務狀態",
            tuple(sorted(repositories.ACTIVE_TASK_STATUSES)) + ("已完成",),
            "/任務總覽、截止提醒、每日催辦、發文待確認清單會整個讀取失敗",
        ),
        OptionCheck(
            config.DS_SUBMISSIONS, "審核狀態",
            ("待審核", *repositories.REVIEW_RESULT_STATUSES),
            "/審核提交、審核結果通知、待審核催辦會整個讀取失敗",
        ),
        OptionCheck(
            config.DS_PEOPLE, "身份類型", ("老師", "公司成員"),
            "排課時選不到老師",
        ),
        OptionCheck(
            config.DS_AUDIT_LOG, "結果", ("成功",),
            "重要操作的防重複檢查會失敗",
        ),
        OptionCheck(
            config.DS_COURSE_MEETINGS, "會議類型", tuple(tenant.qa_hidden_meeting_types),
            "小律排除清單裡的類型在 Notion 不存在。若是被改名，改名後的類型目前不會被擋，"
            "勾了「藝人可見」的那類會議紀錄藝人查得到；請確認後更新 QA_HIDDEN_MEETING_TYPES",
        ),
        OptionCheck(
            # 2026-10-18 Johnny 拍板保留（推翻 09/22「可移除」的行動項目）：它管的是小律能不能
            # 把會議內容講給藝人聽，不是藝人能不能進 Notion。欄位被刪，小律的查詢會整個失敗。
            config.DS_COURSE_MEETINGS, "藝人可見", (),
            "小律查不到任何會議紀錄（查詢會整個失敗）；這個欄位決定哪些會議內容可以講給藝人聽，請勿刪除",
        ),
    ]


def missing_options(check: OptionCheck, schema: dict) -> list[str] | None:
    """回傳 Notion 上不存在的選項；欄位整個不存在回 None。"""
    prop = (schema.get("properties") or {}).get(check.property_name)
    if prop is None:
        return None
    kind = prop.get("type")
    available = {option.get("name") for option in (prop.get(kind) or {}).get("options", [])}
    return [option for option in check.options if option not in available]


def warning_lines(check: OptionCheck, missing: list[str] | None) -> list[str]:
    if missing is None:
        return [f"・`{check.property_name}` 欄位不存在 → {check.impact}"]
    if not missing:
        return []
    return [f"・`{check.property_name}` 少了選項 {'、'.join(missing)} → {check.impact}"]


async def find_problems() -> list[str]:
    lines: list[str] = []
    schemas: dict[str, dict] = {}
    meeting_qa_enabled = config.get_tenant().meeting_qa_enabled
    for check in checks():
        if not check.data_source:
            continue
        # 會議紀錄查詢封存時，會議紀錄的欄位怎麼改都不影響小律，不必警告。
        if not meeting_qa_enabled and check.data_source == config.DS_COURSE_MEETINGS:
            continue
        if check.data_source not in schemas:
            schemas[check.data_source] = await notion_api.retrieve_data_source(check.data_source)
        lines.extend(warning_lines(check, missing_options(check, schemas[check.data_source])))
    return lines


def alert_message(lines: list[str]) -> str:
    return (
        ops_notice.system_title("Notion 選項和程式對不上") + "（bot 啟動檢查）\n"
        + "\n".join(lines)
        + "\n\n通常是 Notion 的選項被刪除或改名。改回原本的名稱，或通知開發端更新程式。"
    )
