"""本機 web 服務（aiohttp，與 bot 同 process 同 event loop）。

只暴露 /discord/oauth/callback 一個路由，供 `/伺服器初始化` 同步各頻道的
slash command 可見範圍時接住 Discord 的授權回呼。只需要 127.0.0.1 本機可達，
不需要對外網域或 Tunnel；未設定 DISCORD_CLIENT_SECRET 時整個服務不啟動。
"""
import logging
import time

from aiohttp import web as aioweb

import config
from cogs import server_setup

logger = logging.getLogger(__name__)

def _html_response(body: str, status: int = 200) -> aioweb.Response:
    return aioweb.Response(text=body, content_type="text/html", status=status)


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


def build_app(client) -> aioweb.Application:
    app = aioweb.Application()
    app["client"] = client
    app.add_routes([
        aioweb.get("/discord/oauth/callback", handle_discord_oauth_callback),
    ])
    return app


async def start(client) -> aioweb.AppRunner | None:
    """沒有 DISCORD_CLIENT_SECRET＝跑不了 OAuth 授權流程，不需要啟動這個服務。"""
    if not config.get_tenant().discord_client_secret:
        logger.info("本機 web 服務未啟用（DISCORD_CLIENT_SECRET 未設定）")
        return None
    port = config.get_tenant().web_port
    runner = aioweb.AppRunner(build_app(client))
    await runner.setup()
    site = aioweb.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("本機 web 服務已啟動：http://127.0.0.1:%s（僅供 Discord OAuth 回呼）", port)
    return runner
