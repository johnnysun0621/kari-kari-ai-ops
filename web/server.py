"""老師確認 web 服務（aiohttp，與 bot 同 process 同 event loop）。

只暴露 /confirm/{token} 兩個路由；token 由 services/teacher_confirm 簽名驗證。
老師確認後只回寫可行時段並通知控方頻道，正式定案仍由控方在 Discord／Notion
手動完成（不會自動發通告）。TenantConfig.teacher_link_secret 未設定時
整個服務不啟動（優雅停用）。
"""
import logging
import time

from aiohttp import web as aioweb

import config
from services import teacher_confirm

from web import pages

logger = logging.getLogger(__name__)

def _html_response(body: str, status: int = 200) -> aioweb.Response:
    return aioweb.Response(text=body, content_type="text/html", status=status)


def _verify_task_token(token: str):
    check = teacher_confirm.verify_token(token, int(time.time()))
    if not check.ok:
        return check, pages.result_html(check.error)
    return check, None


async def handle_confirm_get(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    check, error_page = _verify_task_token(token)
    if error_page:
        return _html_response(error_page)
    payload = check.payload
    windows = [(index, str(label)) for index, label in enumerate(payload.get("windows", []))]
    if not windows:
        return _html_response(pages.result_html("no_candidates"))
    return _html_response(pages.confirm_page_html(
        request_name=str(payload.get("task_name", "課程安排")),
        course_type=str(payload.get("course_type", "課程")),
        duration_text=str(payload.get("duration_text", "")),
        teacher=str(payload.get("teacher", "老師")),
        participants=[str(name) for name in payload.get("participants", [])],
        windows=windows,
        action_url=f"/confirm/{token}",
    ))


async def handle_confirm_post(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    check, error_page = _verify_task_token(token)
    if error_page:
        return _html_response(error_page)
    payload = check.payload
    client = request.app["client"]
    tenant = config.get_tenant()
    form = await request.post()
    action = str(form.get("action", ""))
    choices = list(dict.fromkeys(str(value) for value in form.getall("choice")))
    if action not in {"confirm", "none"} or (action == "confirm" and not choices):
        return _html_response(pages.result_html("invalid"), status=400)

    teacher_confirm.mark_token_used(token, check.expires_at_ts)
    windows = [str(label) for label in payload.get("windows", [])]
    internal = client.get_channel(tenant.admin_notify_channel_id) if client else None
    if internal is not None:
        if action == "none":
            message = (
                f"🔴 **老師回覆：候選時段都無法配合**\n"
                f"課程 Task：{payload.get('task_name', '未命名')}\n"
                "請重新比對或重新蒐集時段。"
            )
        else:
            try:
                selected = [windows[int(choice)] for choice in choices]
            except (ValueError, IndexError):
                teacher_confirm.unmark_token(token)
                return _html_response(pages.result_html("invalid"), status=400)
            message = (
                f"📩 **老師已回覆可行時段，請控方最後定案**\n"
                f"課程 Task：{payload.get('task_name', '未命名')}\n"
                f"老師：{payload.get('teacher', '老師')}\n"
                "老師可行時段：\n"
                + "\n".join(f"・{window}" for window in selected)
                + "\n"
                f"Task：https://www.notion.so/{str(check.resource_page_id).replace('-', '')}"
            )
        try:
            await internal.send(message)
        except Exception:
            logger.exception("老師 Task 表單通知發送失敗")
    return _html_response(pages.result_html("none_ok" if action == "none" else "teacher_selected"))


def build_app(client) -> aioweb.Application:
    app = aioweb.Application()
    app["client"] = client
    app.add_routes([
        aioweb.get("/confirm/{token}", handle_confirm_get),
        aioweb.post("/confirm/{token}", handle_confirm_post),
    ])
    return app


async def start(client) -> aioweb.AppRunner | None:
    """secret 未設定＝不啟動（租戶可不啟用此功能）。"""
    if not teacher_confirm.is_enabled():
        logger.info("老師確認 web 服務未啟用（TEACHER_LINK_SECRET 未設定）")
        return None
    port = config.get_tenant().web_port
    runner = aioweb.AppRunner(build_app(client))
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("老師確認 web 服務已啟動：http://127.0.0.1:%s（對外經 Tunnel）", port)
    return runner
