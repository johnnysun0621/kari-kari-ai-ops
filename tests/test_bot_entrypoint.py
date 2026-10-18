def test_bot_entrypoint_imports_after_legacy_scheduling_removal():
    import bot

    assert bot.KariKariBot is not None


def test_setup_logging_writes_errors_to_file(tmp_path):
    """bot 以 pythonw 執行沒有主控台；logger.exception 要落到檔案，事後才查得到。"""
    import logging

    import bot

    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    try:
        log_path = tmp_path / "logs" / "bot.log"
        bot.setup_logging(log_path)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("cogs.tasks_artist").exception("讀取藝人任務失敗")
        for handler in root.handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "讀取藝人任務失敗" in text and "RuntimeError: boom" in text
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = saved
