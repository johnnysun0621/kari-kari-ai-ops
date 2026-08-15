"""老師確認 web 服務（aiohttp，與 bot 同 process 同 event loop）。

只暴露 /confirm/{token} 兩個路由；token 由 services/teacher_confirm 簽名驗證。
老師確認後只回寫可行時段並通知控方頻道，正式定案仍由控方在 Discord／Notion
手動完成（不會自動發通告）。TenantConfig.teacher_link_secret 未設定時
整個服務不啟動（優雅停用）。
"""
import logging
import time
from urllib.parse import urlencode
import time

from aiohttp import web as aioweb

import config
from services import scheduling_sla, teacher_confirm

from web import pages
from cogs import server_setup

logger = logging.getLogger(__name__)

def _html_response(body: str, status: int = 200) -> aioweb.Response:
    return aioweb.Response(text=body, content_type="text/html", status=status)


def _verify_task_token(token: str):
    check = teacher_confirm.verify_token(token, int(time.time()))
    if not check.ok:
        return check, pages.result_html(check.error)
    return check, None


async def handle_discord_oauth_callback(request: aioweb.Request) -> aioweb.Response:
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    bot = request.app["client"]
    pending = bot.discord_oauth_states.pop(state, None)
    if not pending or pending["expires_at"] < time.time():
        return _html_response("Discord 授權連結已失效，請回 Discord 重新執行初始化。", status=400)
    if not code:
        return _html_response("未取得 Discord 授權碼，請回 Discord 重新執行初始化。", status=400)
    tenant = config.get_tenant()
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                "https://discord.com/api/oauth2/token",
                data={
                    "client_id": tenant.discord_client_id,
                    "client_secret": tenant.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": tenant.discord_oauth_redirect_uri,
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
        guild = bot.get_guild(pending["guild_id"])
        if guild is None:
            return _html_response("找不到原本的 Discord 伺服器，請重新初始化。", status=400)
        await server_setup.sync_command_permissions(
            bot,
            guild,
            pending["channel_ids"],
            pending["admin_role_id"],
            access_token,
        )
    except Exception:
        logger.exception("Discord OAuth 指令權限同步失敗")
        return _html_response("Discord 授權成功，但指令權限同步失敗，請回 Discord 再試一次。", status=502)
    return _html_response(
        "Discord 授權完成。各頻道的 slash command 可見範圍已同步，現在可以關閉此頁面。"
    )


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

    windows = [str(label) for label in payload.get("windows", [])]
    selected: list[str] = []
    if action == "confirm":
        try:
            selected = [windows[int(choice)] for choice in choices]
        except (ValueError, IndexError):
            return _html_response(pages.result_html("invalid"), status=400)
    teacher_confirm.mark_token_used(token, check.expires_at_ts)
    scheduling_sla.mark_teacher_responded(check.resource_page_id, int(time.time()))
    internal = client.get_channel(tenant.admin_notify_channel_id) if client else None
    if internal is not None:
        if action == "none":
            message = (
                f"🔴 **老師回覆：候選時段都無法配合**\n"
                f"課程 Task：{payload.get('task_name', '未命名')}\n"
                "請重新比對或重新蒐集時段。"
            )
        else:
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
        aioweb.get("/discord/oauth/callback", handle_discord_oauth_callback),
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
