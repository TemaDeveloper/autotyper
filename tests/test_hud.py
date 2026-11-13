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


def test_pump_reads_updates_and_returns_linger():
    from autotyper.platform.hud import pump

    seen = []
    lines = ['{"state": "typing"}\n', "not json\n", "[1, 2]\n", '{"linger": "abc"}\n', '{"state": "stopped", "linger": 2}\n']
    assert pump(lines, seen.append) == 2.0
    assert [d.get("state") for d in seen] == ["typing", None, "stopped"]
    assert pump(['{"linger": 999}'], lambda d: None) == 30.0  # capped
    assert pump([], lambda d: None) == 0.0


class _FakeStdin:
    def __init__(self):
        self.closed = False
        self.written = ""

    def write(self, s):
        self.written += s

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakeProc:
    def __init__(self, wait_error=None):
        self.stdin = _FakeStdin()
        self.wait_error = wait_error
        self.killed = False

    def wait(self, timeout=None):
        if self.wait_error and not self.killed:
            raise self.wait_error
        return 0

    def kill(self):
        self.killed = True


def _client(proc):
    from autotyper.platform.hud import HudClient

    c = HudClient.__new__(HudClient)
    c.proc = proc
    return c


def test_close_sends_linger_and_is_idempotent():
    proc = _FakeProc()
    c = _client(proc)
    c.close(linger=2.0)
    assert '"linger": 2.0' in proc.stdin.written and proc.stdin.closed and not proc.killed
    c.close()  # second call (e.g. from atexit) does nothing
    assert c.proc is None


def test_ctrl_c_while_lingering_kills_overlay():
    proc = _FakeProc(wait_error=KeyboardInterrupt())
    _client(proc).close(linger=2.0)
    assert proc.killed


def test_overlay_that_does_not_exit_is_killed():
    import subprocess

    proc = _FakeProc(wait_error=subprocess.TimeoutExpired("hud", 5))
    _client(proc).close(linger=2.0)
    assert proc.killed
