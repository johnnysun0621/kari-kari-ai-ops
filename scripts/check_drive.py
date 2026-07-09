"""Drive 連線驗證：確認 token 有效、看得到總資料夾與藝人專區。

用法：
    .venv/bin/python scripts/check_drive.py
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TOKEN_PATH = Path(os.getenv("GOOGLE_OAUTH_TOKEN_PATH", "credentials/token.json"))
ROOT_ID = os.getenv("GOOGLE_DRIVE_ROOT_FOLDER_ID", "")


def main() -> int:
    if not TOKEN_PATH.exists():
        print(f"❌ 找不到 token：{TOKEN_PATH}——先跑 scripts/authorize_drive.py")
        return 1
    if not ROOT_ID:
        print("❌ .env 缺 GOOGLE_DRIVE_ROOT_FOLDER_ID")
        return 1

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    service = build("drive", "v3", credentials=creds)

    root = service.files().get(fileId=ROOT_ID, fields="id,name,mimeType").execute()
    print(f"✅ 總資料夾可存取：{root['name']}")

    children = service.files().list(
        q=f"'{ROOT_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)",
        pageSize=50,
    ).execute().get("files", [])
    if children:
        print(f"✅ 底下的藝人專區（{len(children)} 個）：")
        for f in children:
            print(f"   ・{f['name']}  →  https://drive.google.com/drive/folders/{f['id']}")
        print("\n把上面各專區連結填入 Notion Artists 的「Google Drive 資料夾」欄位。")
    else:
        print("⚠️ 總資料夾底下還沒有子資料夾——請先建立各藝人專區（例：振華專區、Chenhua專區）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
