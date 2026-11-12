from autotyper.config import Settings
from autotyper.platform.hud import HudModel


def test_typing_progress_across_files():
    m = HudModel()
    v = m.apply({"state": "typing", "msg": "Typing", "file": "B.kt", "index": 2, "count": 4, "line": 25, "total": 50})
    assert v.title.endswith("Typing")
    assert v.file_line == "B.kt  ·  file 2/4  ·  line 25/50"
    assert abs(v.fraction - (1 + 0.5) / 4) < 1e-9


def test_pause_keeps_file_and_shows_reason():
    m = HudModel()
    m.apply({"state": "typing", "file": "A.kt", "index": 1, "count": 1, "line": 3, "total": 10})
    v = m.apply({"state": "waiting", "msg": "Safari is in front - come back to Android Studio"})
    assert v.title.endswith("Paused")
    assert "Safari" in v.detail and v.file_line.startswith("A.kt")


def test_done_is_full_and_unknown_state_is_safe():
    m = HudModel()
    assert m.apply({"state": "done", "msg": "2 file(s) written"}).fraction == 1.0
    assert m.apply({"state": "weird"}).title.endswith("Working")


def test_hud_setting_and_flag():
    from autotyper import cli

    assert Settings().hud is True
    args = cli.build_parser().parse_args(["type", "--no-hud"])
    assert cli._settings(args).hud is False
