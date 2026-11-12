"""Focus gate: decides, before every keystroke, whether it is safe to type.

Typing is allowed only when ALL hold:
  1. the IDE (Android Studio) is the frontmost app,
  2. an IDE window is on screen on the current desktop (Space),
  3. the IDE window title does not name a *different* file than the target,
  4. you are not using the keyboard/mouse yourself (idle >= resume_idle),
  5. you have not paused with the hotkey.

``wait_ready`` blocks until that is true and returns ``True`` when typing had
to pause - the engine then re-syncs with the file on disk, because a click
while you were away may have moved the caret.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol


class Aborted(Exception):
    """User pressed the abort hotkey or Ctrl+C."""


@dataclass
class FocusState:
    frontmost_bundle: str | None
    frontmost_name: str | None
    ide_window_on_screen: bool
    window_title: str | None


class FocusProbe(Protocol):
    def state(self, ide_bundle_ids: list[str]) -> FocusState: ...


class InputMonitor(Protocol):
    available: bool
    paused_by_hotkey: bool
    abort_requested: bool

    def last_user_event_time(self) -> float:
        """time.monotonic() of the user's last real key/click (0.0 if none)."""


class NullMonitor:
    """Used when Input Monitoring permission is missing: no hotkeys, no interference detection."""

    available = False
    paused_by_hotkey = False
    abort_requested = False

    def last_user_event_time(self) -> float:
        return 0.0


_FILE_TOKEN = re.compile(r"[\w.\-]+\.(?:kt|kts|java|xml|gradle|toml|json|properties|pro|md|txt)\b")


def title_names_other_file(title: str | None, target_path: str) -> bool:
    """True if the window title clearly shows another file. Unknown titles -> False."""
    if not title:
        return False
    names = _FILE_TOKEN.findall(title)
    if not names:
        return False
    return os.path.basename(target_path) not in names


StatusFn = Callable[[str, str], None]  # (state, message); state in {"typing","waiting","info"}


class FocusGate:
    def __init__(
        self,
        probe: FocusProbe,
        monitor: InputMonitor,
        ide_bundle_ids: list[str],
        resume_idle: float = 1.2,
        poll_interval: float = 0.15,
        auto_refocus: bool = True,
        refocus_after: float = 4.0,
        on_status: StatusFn | None = None,
        on_refocus: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.probe = probe
        self.monitor = monitor
        self.ide_bundle_ids = ide_bundle_ids
        self.resume_idle = resume_idle
        self.poll_interval = poll_interval
        self.auto_refocus = auto_refocus
        self.refocus_after = refocus_after
        self.on_status = on_status or (lambda state, msg: None)
        self.on_refocus = on_refocus
        self.clock = clock
        self.sleep = sleep
        self._last_ready_check = 0.0
        self._last_reason: str | None = None

    # ------------------------------------------------------------------
    def blocking_reason(self, target_path: str | None, check_title: bool) -> tuple[str | None, str]:
        """Return (reason, kind) - reason None means ready."""
        if self.monitor.abort_requested:
            raise Aborted()
        if self.monitor.paused_by_hotkey:
            return "Paused by hotkey - press it again to resume.", "hotkey"
        st = self.probe.state(self.ide_bundle_ids)
        if st.frontmost_bundle not in self.ide_bundle_ids:
            who = st.frontmost_name or st.frontmost_bundle or "another app"
            return f"{who} is in front - go back to Android Studio to continue.", "app"
        if not st.ide_window_on_screen:
            return "Android Studio is on another desktop - switch back to continue.", "space"
        if check_title and target_path and title_names_other_file(st.window_title, target_path):
            return f"Another file is open - switch to {os.path.basename(target_path)} to continue.", "file"
        idle = self.clock() - self.monitor.last_user_event_time()
        if self.monitor.available and idle < self.resume_idle:
            return "You're using the keyboard/mouse - typing resumes when you stop.", "input"
        return None, ""

    def wait_ready(self, target_path: str | None = None, check_title: bool = True) -> bool:
        """Block until typing is safe. Returns True if we paused (caller should re-sync)."""
        paused = False
        # Input that arrived since the last keystroke we sent = user touched the IDE.
        if self.monitor.available and self.monitor.last_user_event_time() > self._last_ready_check:
            paused = True
        wrong_file_since: float | None = None
        while True:
            reason, kind = self.blocking_reason(target_path, check_title)
            if reason is None:
                break
            paused = True
            if reason != self._last_reason:
                self.on_status("waiting", reason)
                self._last_reason = reason
            if kind == "file" and self.auto_refocus and self.on_refocus and target_path:
                now = self.clock()
                wrong_file_since = wrong_file_since or now
                idle = now - self.monitor.last_user_event_time()
                if now - wrong_file_since >= self.refocus_after and idle >= self.refocus_after:
                    self.on_status("info", f"Reopening {os.path.basename(target_path)}")
                    self.on_refocus(target_path)
                    wrong_file_since = None
            else:
                wrong_file_since = None
            self.sleep(self.poll_interval)
        if self._last_reason is not None:
            self.on_status("typing", "Back in Android Studio - resuming.")
            self._last_reason = None
        self._last_ready_check = self.clock()
        return paused

    def mark_sent(self) -> None:
        """Call right after posting synthetic keys so they are not counted as user input."""
        self._last_ready_check = self.clock()
