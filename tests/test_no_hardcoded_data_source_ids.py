"""守門測試：公司特定值不得寫死在程式碼或範本裡（鐵則 1）。

這個 repo 是 public。config.py 曾經把九組正式環境的 Notion Data Source ID
當成 os.getenv 的 fallback 預設值，.env.example 也帶著同一批真實 ID。
因為 load_dotenv() 必定覆蓋，那些值永遠執行不到，卻把客戶的資料庫結構公開出去，
而且與 .env 實際值已經不一致——真正的風險是 .env 漏載時 bot 默默連到錯誤的九個
資料庫，而不是大聲失敗。
"""
import re
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent.parent

# Notion ID：32 碼 hex，可有可無連字號。開頭鎖 8 碼避免誤抓 commit hash 等短字串。
_NOTION_ID = re.compile(
    r"\b[0-9a-f]{8}-?(?:[0-9a-f]{4}-?){3}[0-9a-f]{12}\b", re.IGNORECASE
)


def _ids_in(text: str) -> set[str]:
    return {m.group(0).replace("-", "").lower() for m in _NOTION_ID.finditer(text)}


class TestNoHardcodedDataSourceIds:
    def test_config_has_no_notion_id_literals(self):
        """config.py 不得出現任何 Notion ID 字面值。"""
        found = _ids_in((ROOT / "config.py").read_text(encoding="utf-8"))

        assert not found, (
            f"config.py 出現寫死的 Notion ID {sorted(found)}；"
            "公司特定值要從 .env 注入（鐵則 1），這個 repo 是 public。"
        )

    def test_env_example_has_no_notion_id_literals(self):
        """.env.example 是範本，只能有空值或佔位字串，不能是真實 ID。"""
        found = _ids_in((ROOT / ".env.example").read_text(encoding="utf-8"))

        assert not found, f".env.example 出現真實 Notion ID {sorted(found)}，請改成空值。"

    def test_every_data_source_field_is_env_backed(self):
        """TenantConfig 的每個 ds_* 欄位都要在對照表裡，新增資料源不會漏掉檢查。"""
        ds_fields = {
            name for name in config.TenantConfig.__dataclass_fields__ if name.startswith("ds_")
        }

        assert ds_fields == set(config.DATA_SOURCE_ENV_VARS), (
            "ds_* 欄位與 DATA_SOURCE_ENV_VARS 不一致；"
            f"只在欄位: {sorted(ds_fields - set(config.DATA_SOURCE_ENV_VARS))}，"
            f"只在對照表: {sorted(set(config.DATA_SOURCE_ENV_VARS) - ds_fields)}"
        )


class TestMissingDataSourceEnv:
    def _tenant(self, **overrides):
        base = {name: "x" * 32 for name in config.DATA_SOURCE_ENV_VARS}
        base.update(overrides)
        return config.TENANT.__class__(
            **{
                **{
                    field: getattr(config.TENANT, field)
                    for field in config.TenantConfig.__dataclass_fields__
                },
                **base,
            }
        )

    def test_reports_nothing_when_all_present(self):
        assert config.missing_data_source_env(self._tenant()) == []

    def test_reports_env_var_names_for_blank_fields(self):
        tenant = self._tenant(ds_tasks="", ds_bulletin="   ")

        assert config.missing_data_source_env(tenant) == [
            "NOTION_DS_BULLETIN",
            "NOTION_DS_TASKS",
        ]

    def test_live_tenant_is_fully_configured(self):
        """本機 .env 必須九組齊全，否則 bot 啟動會被 bot.py 擋下。"""
        assert config.missing_data_source_env() == []
