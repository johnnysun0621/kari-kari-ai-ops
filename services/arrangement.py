"""時間池候選的正式安排分流規則。"""

ARRANGEMENT_TYPES = ("課程", "錄音", "通告", "其他")

# 目前 Tasks 的既有 select 選項沒有「通告／其他」，先映射到可用的營運類型。
# 顯示名稱仍寫入任務說明，未來租戶 schema 可再改成設定化 mapping。
TASK_TYPE_BY_ARRANGEMENT = {
    "錄音": "錄音",
    "通告": "演出",
    "其他": "行政",
}


def requires_teacher_confirmation(arrangement_type: str) -> bool:
    return arrangement_type == "課程"


def creates_unpublished_task_draft(arrangement_type: str) -> bool:
    return arrangement_type in {"錄音", "通告", "其他"}


def task_type_for_arrangement(arrangement_type: str) -> str | None:
    return TASK_TYPE_BY_ARRANGEMENT.get(arrangement_type)
