from types import SimpleNamespace

from cogs import server_setup


def test_channel_specs_cover_required_operational_areas():
    specs = server_setup.channel_specs()

    assert {item.name for item in specs} == {
        "公告區",
        "時間資訊彙整",
        "任務查詢入口",
        "課程助教",
        "營運內部",
    }
    assert next(item for item in specs if item.name == "公告區").read_only is True
    assert next(item for item in specs if item.name == "營運內部").admin_only is True


def test_env_summary_uses_created_resource_ids():
    text = server_setup.env_summary(
        guild_id=1,
        admin_role_id=2,
        channel_ids={
            "公告區": 10,
            "時間資訊彙整": 11,
            "任務查詢入口": 12,
            "課程助教": 13,
            "營運內部": 14,
        },
    )

    assert "GUILD_ID=1" in text
    assert "ADMIN_ROLE_ID=2" in text
    assert "ANNOUNCE_CHANNEL_ID=10" in text
    assert "SCHEDULE_CHANNEL_ID=11" in text
    assert "TASK_CHANNEL_ID=12" in text
    assert "AI_CHANNEL_ID=13" in text
    assert "ADMIN_NOTIFY_CHANNEL_ID=14" in text
    assert "BULLETIN_CHANNEL_ID=10" in text


def test_operational_channels_are_hidden_from_unassigned_members():
    everyone = object()
    artist = object()
    admin = object()
    bot_member = object()
    guild = SimpleNamespace(default_role=everyone)
    spec = next(item for item in server_setup.channel_specs() if item.name == "時間資訊彙整")

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
    spec = next(item for item in server_setup.channel_specs() if item.name == "營運內部")

    overwrites = server_setup._overwrites(guild, admin, artist, bot_member, spec)

    assert overwrites[everyone].view_channel is False
    assert overwrites[artist].view_channel is False
    assert overwrites[admin].view_channel is True
