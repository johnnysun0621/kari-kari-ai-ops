"""一次性授權：產生 Google Drive OAuth token（完整教學見 DRIVE_OAUTH_SETUP.md）。

用法：
    .venv/bin/python scripts/authorize_drive.py

會開瀏覽器讓你登入 Google 同意授權，成功後把 token 寫到
GOOGLE_OAUTH_TOKEN_PATH（預設 credentials/token.json）。
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# 完整 drive scope：bot 要寫進「你手動建立」的藝人專區資料夾，
# 舊 smoke test 的 drive.file（只能碰 app 自建檔案）不夠用。
SCOPES = ["https://www.googleapis.com/auth/drive"]

CLIENT_PATH = Path(os.getenv("GOOGLE_OAUTH_CLIENT_PATH", "credentials/client_secret.json"))
TOKEN_PATH = Path(os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "credentials/token.json"))


def main() -> int:
    if not CLIENT_PATH.exists():
        print(f"❌ 找不到 OAuth 用戶端檔案：{CLIENT_PATH}")
        print("   請照 DRIVE_OAUTH_SETUP.md 步驟一，把 client_secret.json 放到 credentials/")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_PATH), SCOPES)
    print("🔑 即將開啟瀏覽器，請用「擁有總資料夾的 Google 帳號」登入並同意授權…")
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"✅ Token 已儲存：{TOKEN_PATH}")
    print("   接著跑 scripts/check_drive.py 驗證能不能看到總資料夾。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
