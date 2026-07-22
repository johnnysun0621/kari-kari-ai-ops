"""老師確認 web 服務（aiohttp，與 bot 同 process 同 event loop）。

只暴露 /confirm/{token} 兩個路由；token 由 services/teacher_confirm 簽名驗證，
確認動作先回寫老師可行時段；最後由控方經 services/scheduling_flow 落地。TenantConfig.teacher_link_secret
未設定時整個服務不啟動（優雅停用）。
"""
import logging
import time

from aiohttp import web as aioweb

import config
from services import repositories, teacher_confirm, timeparse

from web import pages

logger = logging.getLogger(__name__)

CONFIRMABLE_STATUSES = ("等待老師確認", "比對中", "已確認")


def _html_response(body: str, status: int = 200) -> aioweb.Response:
    return aioweb.Response(text=body, content_type="text/html", status=status)


async def _load_confirmable_request(token: str):
    """共用前置：驗 token；Task 表單不再讀已刪除的舊排課資料庫。"""
    check = teacher_confirm.verify_token(token, int(time.time()))
    if not check.ok:
        return check, None, pages.result_html(check.error)
    if check.payload and check.payload.get("kind") == "task":
        return check, None, None
    request = await repositories.get_request(check.request_page_id)
    if request is None:
        return check, None, pages.result_html("invalid")
    if request.status == "已發布通告":
        return check, request, pages.result_html("already")
    if request.status not in CONFIRMABLE_STATUSES:
        return check, request, pages.result_html("invalid")
    return check, request, None


async def handle_confirm_get(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    check, sched_request, error_page = await _load_confirmable_request(token)
    if check.ok and check.payload and check.payload.get("kind") == "task":
        payload = check.payload
        windows = [
            (index, str(label))
            for index, label in enumerate(payload.get("windows", []))
        ]
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
    if error_page:
        return _html_response(error_page)
    candidates = timeparse.parse_candidates_text(sched_request.candidates_text)
    candidates = repositories.split_windows_by_duration(
        candidates, sched_request.duration_minutes
    )
    windows = [(i, timeparse.humanize(s, e)) for i, (s, e) in enumerate(candidates)]
    if not windows:
        return _html_response(pages.result_html("no_candidates"))
    return _html_response(pages.confirm_page_html(
        request_name=sched_request.name,
        course_type=sched_request.course_type or "課程",
        duration_text=repositories.format_duration_hours(sched_request.duration_minutes),
        teacher=sched_request.teacher,
        windows=windows,
        action_url=f"/confirm/{token}",
    ))


async def handle_confirm_post(request: aioweb.Request) -> aioweb.Response:
    token = request.match_info["token"]
    check, sched_request, error_page = await _load_confirmable_request(token)
    if check.ok and check.payload and check.payload.get("kind") == "task":
        payload = check.payload
        form = await request.post()
        action = str(form.get("action", ""))
        choices = list(dict.fromkeys(str(value) for value in form.getall("choice")))
        windows = [str(label) for label in payload.get("windows", [])]
        if action not in {"confirm", "none"} or (action == "confirm" and not choices):
            return _html_response(pages.result_html("invalid"), status=400)
        teacher_confirm.mark_token_used(token, check.expires_at_ts)
        client = request.app["client"]
        tenant = config.get_tenant()
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
                    f"Task：https://www.notion.so/{str(check.request_page_id).replace('-', '')}"
                )
            try:
                await internal.send(message)
            except Exception:
                logger.exception("老師 Task 表單通知發送失敗")
        return _html_response(pages.result_html("none_ok" if action == "none" else "teacher_selected"))
    if error_page:
        return _html_response(error_page)
    client = request.app["client"]
    tenant = config.get_tenant()
    form = await request.post()
    action = str(form.get("action", ""))
    choices = list(dict.fromkeys(str(value) for value in form.getall("choice")))
    if action not in {"confirm", "none"} or (action == "confirm" and not choices):
        return _html_response(pages.result_html("invalid"), status=400)

    # 先燒掉 token（防連點/重送）；流程失敗再還原讓老師重試
    teacher_confirm.mark_token_used(token, check.expires_at_ts)

    if action == "none":
        internal = client.get_channel(tenant.admin_notify_channel_id) if client else None
        if internal is not None:
            try:
                await internal.send(
                    f"🔴 **老師回覆：候選時段都無法配合**\n"
                    f"排課：{sched_request.name}｜老師：{sched_request.teacher or '未填'}\n"
                    f"請重新蒐集時間或改期後再比對。"
                )
            except Exception:
                logger.exception("老師拒絕通知發送失敗")
        return _html_response(pages.result_html("none_ok"))

    candidates = timeparse.parse_candidates_text(sched_request.candidates_text)
    candidates = repositories.split_windows_by_duration(
        candidates, sched_request.duration_minutes
    )
    selected_candidates = []
    try:
        selected_candidates = [candidates[int(choice)] for choice in choices]
    except (ValueError, IndexError):
        teacher_confirm.unmark_token(token)
        return _html_response(pages.result_html("invalid"), status=400)
    try:
        selected_text = "\n".join(
            timeparse.format_candidate(start, end) for start, end in selected_candidates
        )
        await repositories.set_request_candidates(
            sched_request.page_id, selected_text, status="已確認"
        )
    except Exception:
        logger.exception("老師複選時段寫入失敗")
        teacher_confirm.unmark_token(token)  # 還原，讓老師稍後重試
        return _html_response(pages.result_html("error"), status=500)

    internal = client.get_channel(tenant.admin_notify_channel_id) if client else None
    if internal is not None:
        try:
            await internal.send(
                f"📩 **老師已回覆可行時段，請控方最後定案**\n"
                f"排課：{sched_request.name}\n"
                f"老師可行：{'、'.join(timeparse.humanize(start, end) for start, end in selected_candidates)}\n"
                f"請執行 `/排課確認` 選定正式時間。"
            )
        except Exception:
            logger.exception("老師確認通知發送失敗")
    return _html_response(pages.result_html("teacher_selected"))


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
