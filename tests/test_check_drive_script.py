import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_drive.py"
    spec = importlib.util.spec_from_file_location("check_drive_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_utf8_stdout_rewraps_windows_cp950_stream():
    module = _load_script()
    output = io.BytesIO()
    cp950 = io.TextIOWrapper(output, encoding="cp950", line_buffering=True)
    fake_sys = SimpleNamespace(stdout=cp950)
    module.sys = fake_sys

    module._utf8_stdout()
    print("✅ Drive 正常", file=fake_sys.stdout)
    fake_sys.stdout.flush()

    assert fake_sys.stdout.encoding.lower() == "utf-8"
    assert "✅ Drive 正常" in output.getvalue().decode("utf-8")


def test_utf8_stdout_keeps_existing_utf8_stream():
    module = _load_script()
    output = io.BytesIO()
    utf8 = io.TextIOWrapper(output, encoding="utf-8", line_buffering=True)
    fake_sys = SimpleNamespace(stdout=utf8)
    module.sys = fake_sys

    module._utf8_stdout()

    assert fake_sys.stdout is utf8
