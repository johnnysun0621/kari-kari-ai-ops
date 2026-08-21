from types import SimpleNamespace

from cogs import server_setup


def test_channel_specs_cover_required_operational_areas():
    specs = server_setup.channel_specs()

    assert {item.name for item in specs} == {
        "公告區",
        "時間管理",
        "任務查詢入口",
        "課程助教",
        "營運內部通知",
        "營運操作",
    }
    assert next(item for item in specs if item.name == "公告區").read_only is True
    assert next(item for item in specs if item.name == "營運內部通知").admin_only is True


def test_env_summary_uses_created_resource_ids():
    text = server_setup.env_summary(
        guild_id=1,
        admin_role_id=2,
        channel_ids={
            "公告區": 10,
            "時間管理": 11,
            "任務查詢入口": 12,
            "課程助教": 13,
            "營運內部通知": 14,
            "營運操作": 15,
        },
    )

    assert "GUILD_ID=1" in text
    assert "ADMIN_ROLE_ID=2" in text
    assert "ANNOUNCE_CHANNEL_ID=10" in text
    assert "SCHEDULE_CHANNEL_ID=11" in text
    assert "TASK_CHANNEL_ID=12" in text
    assert "AI_CHANNEL_ID=13" in text
    assert "ADMIN_NOTIFY_CHANNEL_ID=14" in text
    assert "ADMIN_COMMAND_CHANNEL_ID=15" in text
    assert "BULLETIN_CHANNEL_ID=10" in text


def test_command_channel_groups_cover_each_slash_command():
    groups = server_setup.command_channel_groups()

    assert groups["時間資訊蒐集"] == ("營運操作", True)
    assert groups["管理可配合時間"] == ("時間管理", False)
    assert groups["我的任務"] == ("任務查詢入口", False)
    assert groups["從時間池建立任務"] == ("營運操作", True)
    assert groups["伺服器初始化"] == ("營運操作", True)


def test_command_permissions_are_scoped_to_one_channel():
    permissions = server_setup.command_permission_entries(100, 200)

    assert permissions == [
        {"id": "99", "type": 3, "permission": False},
        {"id": "200", "type": 3, "permission": True},
    ]


def test_existing_configured_legacy_task_channel_is_reused(monkeypatch):
    legacy = SimpleNamespace(id=123, name="任務專用")
    guild = SimpleNamespace(
        text_channels=[legacy],
        get_channel=lambda channel_id: legacy if channel_id == 123 else None,
    )
    spec = next(item for item in server_setup.channel_specs() if item.name == "任務查詢入口")
    monkeypatch.setattr(server_setup.config, "TASK_CHANNEL_ID", 123)

    assert server_setup.find_existing_channel(guild, spec) is legacy


def test_legacy_task_channel_name_is_reused_without_configured_id(monkeypatch):
    legacy = SimpleNamespace(id=123, name="任務專用")
    guild = SimpleNamespace(text_channels=[legacy], get_channel=lambda _: None)
    spec = next(item for item in server_setup.channel_specs() if item.name == "任務查詢入口")
    monkeypatch.setattr(server_setup.config, "TASK_CHANNEL_ID", 0)

    assert server_setup.find_existing_channel(guild, spec) is legacy


def test_oauth_authorize_url_contains_required_scope_and_state(monkeypatch):
    monkeypatch.setattr(server_setup.config, "get_tenant", lambda: SimpleNamespace(
        discord_client_id="app-123",
        discord_oauth_redirect_uri="http://127.0.0.1:8787/discord/oauth/callback",
    ))

    url = server_setup.oauth_authorize_url("state-123")

    assert "client_id=app-123" in url
    assert "state=state-123" in url
    assert "applications.commands.permissions.update" in url


def test_operational_channels_are_hidden_from_unassigned_members():
    everyone = object()
    artist = object()
    admin = object()
    bot_member = object()
    guild = SimpleNamespace(default_role=everyone)
    spec = next(item for item in server_setup.channel_specs() if item.name == "時間管理")

    overwrites = server_setup._overwrites(guild, admin, artist, bot_member, spec)

    assert overwrites[everyone].view_channel is False
    assert overwrites[artist].view_channel is True
    assert overwrites[admin].view_channel is True
    assert overwrites[bot_member].view_channel is True


def test_announcement_is_read_only_for_artists():
    everyone = object()
    artist = object()
    admin = object()
    bot_member = object()
    guild = SimpleNamespace(default_role=everyone)
    spec = next(item for item in server_setup.channel_specs() if item.name == "公告區")

    overwrites = server_setup._overwrites(guild, admin, artist, bot_member, spec)

    assert overwrites[artist].view_channel is True
    assert overwrites[artist].send_messages is False
    assert overwrites[admin].send_messages is True


def test_internal_channel_is_hidden_from_artists():
    everyone = object()
    artist = object()
    admin = object()
    bot_member = object()
    guild = SimpleNamespace(default_role=everyone)
    spec = next(item for item in server_setup.channel_specs() if item.name == "營運內部通知")

    overwrites = server_setup._overwrites(guild, admin, artist, bot_member, spec)

    assert overwrites[everyone].view_channel is False
    assert overwrites[artist].view_channel is False
    assert overwrites[admin].view_channel is True


def test_operations_channel_is_hidden_from_artists():
    everyone = object()
    artist = object()
    admin = object()
    bot_member = object()
    guild = SimpleNamespace(default_role=everyone)
    spec = next(item for item in server_setup.channel_specs() if item.name == "營運操作")

    overwrites = server_setup._overwrites(guild, admin, artist, bot_member, spec)

    assert overwrites[artist].view_channel is False
    assert overwrites[admin].use_application_commands is True
