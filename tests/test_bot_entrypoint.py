def test_bot_entrypoint_imports_after_legacy_scheduling_removal():
    import bot

    assert bot.KariKariBot is not None
