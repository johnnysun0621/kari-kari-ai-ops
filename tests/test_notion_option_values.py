"""守門測試：程式比對的選項字串，必須是 Notion 既有的選項。

這類錯誤不會報錯，只會讓判斷分支永遠為假。2026-10-03 盤點時找到三個：

- `HARD_BLOCK_TASK_TYPES` 裡的「課程場次」不是有效的任務類型 → 少擋一類
- 程式硬編「課程與作業」，Notion 實際是「課程時間&作業繳交」
  → 課程任務完全不會擋藝人可配合時段，排課會排到藝人已經有課的時間
- 檔案類型對照表的「業配內容」，Notion 實際是「方晨業配內容」
- 2026-10-13：「方晨業配內容」又被客戶改名為「方晨_哈根達斯雪酥」，對照表再次靜默失效。
  之後檔案類型與 Drive 資料夾改以專案類型／專案名稱為主（M4-07、M4-16），不再追著任務類型改

下面的選項清單是 2026-10-04 以 bot 的 integration token 實撈正式環境得到的。
**Notion 端增減選項時要回來同步這份清單**——這個測試的用途就是強迫這件事發生。
撈法見 docs 或：notion_api.retrieve_data_source(config.DS_TASKS)
"""
import config
from services import drive_archive, repositories

# ── 2026-10-04 實撈；任務類型 2026-10-13 重撈 ─────────────────────
TASK_TYPE_OPTIONS = {
    "課程時間&作業繳交", "創作交付", "企劃提案", "方晨_哈根達斯雪酥", "影片素材",
    "錄音", "演出", "行政", "會議溝通", "發布上架", "通告", "音樂製作",
    "方晨像素吉他周邊", "方晨youtube直播",
}
TASK_STATUS_OPTIONS = {
    "尚未開始", "進行中", "待提交", "待審核", "需修改",
    "已完成", "已逾期", "已取消", "已封存", "已完成發文",
}
REMINDER_RULE_OPTIONS = {"3 天前", "1 天前", "當天", "逾期"}
SUBMISSION_FILE_TYPE_OPTIONS = {
    "課程作業", "錄音檔", "Demo", "編曲檔", "混音母帶",
    "業配素材", "企劃書", "合約", "其他",
}
PROJECT_TYPE_OPTIONS = {
    "作品製作", "月度排程 （發文）", "課程培訓", "內容企劃", "商務合作",
    "公司人員排程", "藝人外部商業合作", "月度排程 （行程）", "週任務",
}
# ────────────────────────────────────────────────────────────────


class TestTaskTypeValues:
    def test_hard_block_types_are_real_options(self):
        """硬擋時段的類型全部要是有效選項，否則那一類永遠擋不到。"""
        dead = repositories.HARD_BLOCK_TASK_TYPES - TASK_TYPE_OPTIONS

        assert not dead, f"HARD_BLOCK_TASK_TYPES 有死值 {sorted(dead)}，這些類型永遠不會硬擋"

    def test_no_hardcoded_task_type_literal_outside_options(self):
        """程式裡任何 `task_type == "…"`／`task_type="…"` 的字面字串都必須是有效選項。

        2026-10-10 在 cogs/scheduling_admin.py 又找到兩處寫死「課程與作業」，
        時間池建立課程任務的分支因此永遠不觸發。常數有守門，散落的字面字串沒有，這條補上。
        """
        import re
        from pathlib import Path

        pattern = re.compile(r'task_type\s*(?:==|=)\s*"([^"]+)"')
        dead = []
        for path in sorted(Path("cogs").glob("*.py")) + sorted(Path("services").glob("*.py")):
            for value in pattern.findall(path.read_text(encoding="utf-8")):
                if value not in TASK_TYPE_OPTIONS:
                    dead.append(f"{path}: {value}")
        assert not dead, f"寫死了不存在的任務類型：{dead}"

    def test_course_task_type_is_a_real_option(self):
        """課程類型字串對不上的話，課程完全不會擋藝人時段——2026-10-03 就是這個狀況。"""
        assert repositories.COURSE_TASK_TYPE in TASK_TYPE_OPTIONS

    def test_file_type_mapping_keys_are_real_task_types(self):
        dead = set(repositories.SUBMISSION_FILE_TYPE_BY_TASK_TYPE) - TASK_TYPE_OPTIONS

        assert not dead, f"檔案類型對照表的 key 有死值 {sorted(dead)}，那些任務類型會落到「其他」"

    def test_file_type_mapping_values_are_real_file_types(self):
        bad = set(repositories.SUBMISSION_FILE_TYPE_BY_TASK_TYPE.values()) - SUBMISSION_FILE_TYPE_OPTIONS

        assert not bad, f"檔案類型對照表的 value 不是有效選項 {sorted(bad)}，寫入 Notion 會失敗"

    def test_project_file_type_mapping_uses_real_options(self):
        """M4-16：專案類型對照的 key 要是有效的專案類型、value 要是有效的檔案類型。"""
        dead = set(repositories.SUBMISSION_FILE_TYPE_BY_PROJECT_TYPE) - PROJECT_TYPE_OPTIONS
        bad = set(repositories.SUBMISSION_FILE_TYPE_BY_PROJECT_TYPE.values()) - SUBMISSION_FILE_TYPE_OPTIONS

        assert not dead, f"專案類型對照的 key 有死值 {sorted(dead)}"
        assert not bad, f"專案類型對照的 value 不是有效檔案類型 {sorted(bad)}，寫入 Notion 會失敗"

    def test_drive_folders_no_longer_keyed_by_task_type(self):
        """M4-07：Drive 分夾改看專案，任務類型對照表已退役，不要再加回來。"""
        assert not hasattr(drive_archive, "DEFAULT_TYPE_FOLDERS")

    def test_public_task_types_are_real_options(self):
        """PUBLIC_TASK_TYPES 從 .env 注入，寫不存在的選項就永遠不會進公告區。"""
        configured = set(config.get_tenant().public_task_types)
        dead = configured - TASK_TYPE_OPTIONS

        assert not dead, f"PUBLIC_TASK_TYPES 有死值 {sorted(dead)}，那些類型不會同步到公告區"


class TestProjectTypeValues:
    """`專案類型` 決定任務的分類，而發文／行程那兩個字串是客戶手打的。"""

    def test_compared_project_types_are_real_options(self):
        compared = {
            repositories.PROJECT_TYPE_PUBLISHING,
            repositories.PROJECT_TYPE_ITINERARY,
            repositories.PROJECT_TYPE_COURSE_TRAINING,
            repositories.PROJECT_TYPE_WEEKLY,
        }
        dead = compared - PROJECT_TYPE_OPTIONS

        assert not dead, f"PROJECT_TYPE_* 有死值 {sorted(dead)}，那幾類的判斷永遠為假"

    def test_publishing_and_itinerary_keep_the_exact_spacing(self):
        """半角空格 ＋ 全角括號。整個分類功能靠這兩個字串相符，所以逐字元釘住。

        2026-10-04 以 bot token 實撈 Projects 的選項確認過 codepoints。
        """
        assert repositories.PROJECT_TYPE_PUBLISHING == "月度排程 （發文）"
        assert repositories.PROJECT_TYPE_ITINERARY == "月度排程 （行程）"


class TestTaskStatusValues:
    def test_status_sets_only_contain_real_options(self):
        declared = repositories.ACTIVE_TASK_STATUSES | repositories.INACTIVE_TASK_STATUSES
        dead = declared - TASK_STATUS_OPTIONS

        assert not dead, f"狀態集合有死值 {sorted(dead)}"

    def test_every_notion_status_is_classified(self):
        """每個 Notion 狀態都要落在 ACTIVE 或 INACTIVE。

        2026-10-03 發現 `已完成發文` 兩邊都不在 → 落到該狀態的任務會從 active
        查詢裡靜默消失，也不算終態。客戶新增狀態選項時這個測試要先失敗。
        """
        classified = repositories.ACTIVE_TASK_STATUSES | repositories.INACTIVE_TASK_STATUSES
        unclassified = TASK_STATUS_OPTIONS - classified

        assert not unclassified, (
            f"這些 Notion 狀態沒有歸屬 {sorted(unclassified)}；"
            "既不算進行中也不算終態，會從 active 查詢裡消失"
        )

    def test_submittable_is_a_subset_of_active(self):
        assert repositories.SUBMITTABLE_TASK_STATUSES <= repositories.ACTIVE_TASK_STATUSES

    def test_review_results_are_real_submission_statuses(self):
        """審核結果要是 Submissions「審核狀態」的有效選項。"""
        submission_review_options = {"通過", "待審核", "需修改", "退回", "已封存"}
        dead = set(repositories.REVIEW_RESULT_STATUSES) - submission_review_options

        assert not dead, f"審核結果有死值 {sorted(dead)}"


class TestReminderRuleValues:
    def test_reminder_rule_days_keys_are_known(self):
        """`REMINDER_RULE_DAYS` 可以多於 Notion 選項（預設值在程式裡套用，不經過選項）。

        但多出來的要是刻意的：目前只有 `5 天前`，它是租戶預設的一部分，
        控方在 Notion 勾不到——這個不對稱是已知的，見規則 M3-10。
        """
        extra = set(repositories.REMINDER_RULE_DAYS) - REMINDER_RULE_OPTIONS

        assert extra == {"5 天前"}, (
            f"REMINDER_RULE_DAYS 與 Notion 選項的差集變了：{sorted(extra)}。"
            "多出來的值控方勾不到，只有租戶預設用得上——確認這是刻意的再改這個測試"
        )

    def test_tenant_default_rules_are_recognised(self):
        """租戶預設的每個值都要被 REMINDER_RULE_DAYS 或逾期階段認得，否則發不出來。"""
        known = set(repositories.REMINDER_RULE_DAYS) | {repositories.OVERDUE_STAGE}
        unknown = set(config.get_tenant().default_reminder_rules) - known

        assert not unknown, f"預設提醒規則有程式不認得的值 {sorted(unknown)}，那幾次提醒不會發"
