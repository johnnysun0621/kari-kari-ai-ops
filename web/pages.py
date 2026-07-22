"""老師確認頁 HTML 產生器（純函式，可獨立測試；行動版大按鈕優先）。"""
import html as _html

_STYLE = """
body{font-family:-apple-system,'PingFang TC','Microsoft JhengHei',sans-serif;
     margin:0;padding:24px;background:#f5f6f8;color:#1c1c1e}
.card{max-width:520px;margin:0 auto;background:#fff;border-radius:16px;
      padding:28px 24px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:1.25rem;margin:0 0 4px}
.meta{color:#6b7280;font-size:.95rem;margin-bottom:20px;line-height:1.6}
button.slot{display:block;width:100%;padding:16px;margin:10px 0;font-size:1.05rem;
      border:2px solid #4f46e5;border-radius:12px;background:#fff;color:#4f46e5;
      cursor:pointer;text-align:center}
button.slot:active{background:#eef2ff}
label.slot{white-space:normal}
button.slot input{margin-right:8px;transform:scale(1.25)}
button.none{display:block;width:100%;padding:14px;margin-top:22px;font-size:.95rem;
      border:1px solid #d1d5db;border-radius:12px;background:#fff;color:#6b7280;cursor:pointer}
button.confirm{display:block;width:100%;padding:15px;margin-top:18px;font-size:1rem;
      border:0;border-radius:12px;background:#4f46e5;color:#fff;cursor:pointer}
.big{font-size:2.2rem;text-align:center;margin:8px 0 16px}
.msg{text-align:center;line-height:1.7}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><div class='card'>{body}</div></body></html>"
    )


def confirm_page_html(
    *,
    request_name: str,
    course_type: str,
    duration_text: str,
    teacher: str,
    windows: list[tuple[int, str]],
    action_url: str,
    participants: list[str] | None = None,
) -> str:
    """候選時段複選頁：老師先勾可行時段，再交由控方最後定案。"""
    buttons = "<br>".join(
        f"<label class='slot'><input type='checkbox' name='choice' value='{index}'>"
        f"{_html.escape(label)}</label>"
        for index, label in windows
    )
    body = (
        f"<h1>{_html.escape(request_name)}</h1>"
        f"<div class='meta'>課程類型：{_html.escape(course_type)}｜需要 {_html.escape(duration_text)}<br>"
        f"確認老師：{_html.escape(teacher or '老師')}<br>"
        f"參與藝人：{_html.escape('、'.join(participants or []) or '未指定')}</div>"
        "<div class='meta'>請勾選一個或多個您可以配合的時段。最後由營運決定正式時間，老師回覆不會立即發布通告：</div>"
        f"<form method='post' action='{_html.escape(action_url)}'>{buttons}"
        "<button class='confirm' type='submit' name='action' value='confirm'>"
        "確認送出已勾選時段</button>"
        "<button class='none' type='submit' name='action' value='none'>"
        "以下時段我都無法配合</button></form>"
    )
    return _page(f"確認上課時段｜{request_name}", body)


RESULT_PAGES = {
    "confirmed": ("✅", "已確認！", "正式課程已建立並發布通告。<br>感謝您的協助，此連結已失效。"),
    "teacher_selected": ("📩", "已收到您的時段！", "營運已收到您勾選的時段，會由營運最後確認正式時間。<br>感謝您的協助，此連結已失效。"),
    "none_ok": ("📮", "已收到回覆", "我們已通知營運重新安排時段，會再與您聯繫。"),
    "expired": ("⌛", "連結已過期", "此確認連結已超過有效時間，請聯絡營運重新取得。"),
    "used": ("🔒", "連結已使用過", "這個確認先前已完成；如需變更請直接聯絡營運。"),
    "invalid": ("❌", "連結無效", "請確認網址完整，或聯絡營運重新取得連結。"),
    "already": ("✅", "此排課已確認完成", "無需再次操作；如有疑問請聯絡營運。"),
    "no_candidates": ("🤔", "目前沒有候選時段", "請聯絡營運確認排課狀態。"),
    "error": ("⚠️", "處理失敗", "系統暫時出錯，您的選擇尚未生效，請稍後重開連結再試一次。"),
}


def result_html(kind: str, detail: str = "") -> str:
    icon, title, message = RESULT_PAGES.get(kind, RESULT_PAGES["error"])
    detail_html = f"<div class='msg'><b>{_html.escape(detail)}</b></div>" if detail else ""
    body = f"<div class='big'>{icon}</div><h1 style='text-align:center'>{title}</h1>{detail_html}<div class='msg'>{message}</div>"
    return _page(title, body)
