"""macOS implementations: keyboard (CGEvent), clipboard (NSPasteboard), focus probe
(Accessibility + CGWindowList), file opener (`open -a`), and an input monitor
(listen-only CGEventTap) that detects your own keys/clicks and global hotkeys.

Permissions (System Settings > Privacy & Security) for the terminal app you run
`autotyper` from:
  * Accessibility    - post keystrokes, read the focused window title
  * Input Monitoring - detect your own typing/clicks and the pause/abort hotkeys
"""

from __future__ import annotations

import glob
import os
import subprocess
import threading
import time
from typing import Sequence

import Quartz
from AppKit import (
    NSData,
    NSPasteboard,
    NSPasteboardTypeString,
    NSRunningApplication,
    NSWorkspace,
)
from ApplicationServices import (
    AXIsProcessTrustedWithOptions,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementCreateSystemWide,
    AXUIElementGetPid,
    kAXFocusedApplicationAttribute,
    kAXFocusedWindowAttribute,
    kAXTitleAttribute,
    kAXTrustedCheckOptionPrompt,
)

from ..typer.gate import FocusState

MAGIC = 0x41_55_54_4F  # "AUTO": tags our synthetic events so the monitor ignores them

# US ANSI virtual key codes.
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "`": 50, " ": 49,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "escape": 53,
    "forward_delete": 117, "home": 115, "end": 119, "page_up": 116, "page_down": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
}
_SHIFTED = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\",
    ":": ";", '"': "'", "<": ",", ">": ".", "?": "/",
}
_MODS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
}
_FN_KEYS = {"home", "end", "page_up", "page_down", "forward_delete", "left", "right", "up", "down"}


# --------------------------------------------------------------------- permissions
def accessibility_trusted(prompt: bool = False) -> bool:
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))


def input_monitoring_allowed(request: bool = False) -> bool:
    if request and hasattr(Quartz, "CGRequestListenEventAccess"):
        return bool(Quartz.CGRequestListenEventAccess())
    if hasattr(Quartz, "CGPreflightListenEventAccess"):
        return bool(Quartz.CGPreflightListenEventAccess())
    return True


def post_events_allowed(request: bool = False) -> bool:
    if request and hasattr(Quartz, "CGRequestPostEventAccess"):
        return bool(Quartz.CGRequestPostEventAccess())
    if hasattr(Quartz, "CGPreflightPostEventAccess"):
        return bool(Quartz.CGPreflightPostEventAccess())
    return accessibility_trusted()


# --------------------------------------------------------------------- keyboard
class MacKeyboard:
    def __init__(self, hold: float = 0.004):
        self.src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
        self.hold = hold

    def _post(self, keycode: int, down: bool, flags: int, text: str | None = None) -> None:
        ev = Quartz.CGEventCreateKeyboardEvent(self.src, keycode, down)
        if text is not None:
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(text.encode("utf-16-le")) // 2, text)
        Quartz.CGEventSetFlags(ev, flags)  # explicit flags: your held modifiers never leak in
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGEventSourceUserData, MAGIC)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def type_char(self, ch: str) -> None:
        if ch in ("\n", "\t"):
            raise ValueError("use press() for return/tab")
        flags = 0
        base = ch
        if ch in _SHIFTED:
            base, flags = _SHIFTED[ch], Quartz.kCGEventFlagMaskShift
        elif ch.isascii() and ch.isalpha() and ch.isupper():
            base, flags = ch.lower(), Quartz.kCGEventFlagMaskShift
        keycode = _KEYCODES.get(base, 0)
        self._post(keycode, True, flags, ch)
        time.sleep(self.hold)
        self._post(keycode, False, flags, ch)

    def press(self, key: str, mods: Sequence[str] = ()) -> None:
        if key not in _KEYCODES:
            raise ValueError(f"unknown key {key!r}")
        flags = 0
        for m in mods:
            flags |= _MODS[m]
        if key in _FN_KEYS:
            flags |= Quartz.kCGEventFlagMaskSecondaryFn
        if key in {"left", "right", "up", "down"}:
            flags |= Quartz.kCGEventFlagMaskNumericPad
        code = _KEYCODES[key]
        self._post(code, True, flags)
        time.sleep(self.hold)
        self._post(code, False, flags)


# --------------------------------------------------------------------- clipboard
class MacClipboard:
    TRANSIENT = ("org.nspasteboard.TransientType", "org.nspasteboard.ConcealedType")

    def __init__(self):
        self.pb = NSPasteboard.generalPasteboard()

    def get_text(self) -> str | None:
        s = self.pb.stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else None

    def set_text(self, text: str, transient: bool = True) -> None:
        types = [NSPasteboardTypeString, *(self.TRANSIENT if transient else ())]
        self.pb.declareTypes_owner_(types, None)
        self.pb.setString_forType_(text, NSPasteboardTypeString)
        if transient:
            for t in self.TRANSIENT:
                self.pb.setData_forType_(NSData.data(), t)

    def snapshot(self) -> list[dict[str, bytes]]:
        """Copy every item/type so restoring keeps images, rich text, files..."""
        items = []
        for item in self.pb.pasteboardItems() or []:
            entry = {}
            for t in item.types():
                data = item.dataForType_(t)
                if data is not None:
                    entry[str(t)] = bytes(data)
            items.append(entry)
        return items

    def restore(self, items: list[dict[str, bytes]] | None) -> None:
        from AppKit import NSPasteboardItem

        self.pb.clearContents()
        if not items:
            return
        objs = []
        for entry in items:
            it = NSPasteboardItem.alloc().init()
            for t, data in entry.items():
                it.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), t)
            objs.append(it)
        self.pb.writeObjects_(objs)


# --------------------------------------------------------------------- focus
def _ax_attr(element, attr):
    err, value = AXUIElementCopyAttributeValue(element, attr, None)
    return value if err == 0 else None


class MacFocusProbe:
    def frontmost_pid(self) -> int | None:
        app = _ax_attr(AXUIElementCreateSystemWide(), kAXFocusedApplicationAttribute)
        if app is not None:
            err, pid = AXUIElementGetPid(app, None)
            if err == 0:
                return int(pid)
        # Fallback (can be stale without a run loop - pump it briefly).
        from Foundation import NSDate, NSRunLoop

        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(front.processIdentifier()) if front else None

    def state(self, ide_bundle_ids: list[str]) -> FocusState:
        pid = self.frontmost_pid()
        bundle = name = None
        if pid is not None:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                bundle = str(app.bundleIdentifier() or "") or None
                name = str(app.localizedName() or "") or None
        on_screen = False
        title = None
        if pid is not None and bundle in ide_bundle_ids:
            on_screen = self._has_onscreen_window(pid)
            win = _ax_attr(AXUIElementCreateApplication(pid), kAXFocusedWindowAttribute)
            if win is not None:
                t = _ax_attr(win, kAXTitleAttribute)
                title = str(t) if t is not None else None
        return FocusState(bundle, name, on_screen, title)

    @staticmethod
    def _has_onscreen_window(pid: int) -> bool:
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if int(w.get("kCGWindowOwnerPID", -1)) != pid or int(w.get("kCGWindowLayer", 1)) != 0:
                continue
            b = w.get("kCGWindowBounds") or {}
            if float(b.get("Width", 0)) > 200 and float(b.get("Height", 0)) > 150:
                return True
        return False


# --------------------------------------------------------------------- opener
def find_ide_app(bundle_ids: list[str], configured: str | None = None) -> str | None:
    if configured and os.path.exists(configured):
        return configured
    for bid in bundle_ids:
        try:
            out = subprocess.run(
                ["mdfind", f"kMDItemCFBundleIdentifier == '{bid}'"],
                capture_output=True, text=True, timeout=5,
            ).stdout.split("\n")
        except (OSError, subprocess.TimeoutExpired):
            out = []
        apps = [p for p in out if p.endswith(".app")]
        if apps:
            return sorted(apps, key=lambda p: (not p.startswith("/Applications"), p))[0]
    for pattern in ("/Applications/Android Studio*.app", os.path.expanduser("~/Applications/Android Studio*.app")):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


class MacFileOpener:
    def __init__(self, app_path: str):
        self.app_path = app_path

    def open_file(self, path: str) -> None:
        subprocess.run(["open", "-a", self.app_path, path], check=True)


# --------------------------------------------------------------------- input monitor
# Hotkeys: Ctrl+Option+Cmd+P = pause/resume, Ctrl+Option+Cmd+X = abort.
_HOTKEY_FLAGS = Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate | Quartz.kCGEventFlagMaskCommand
_KEY_P, _KEY_X = 35, 7


class MacInputMonitor:
    def __init__(self):
        self.available = False
        self.paused_by_hotkey = False
        self.abort_requested = False
        self._last = 0.0
        self._tap = None
        self._loop = None
        self._ready = threading.Event()

    def last_user_event_time(self) -> float:
        return self._last

    def start(self) -> bool:
        t = threading.Thread(target=self._run, name="autotyper-input-monitor", daemon=True)
        t.start()
        self._ready.wait(2.0)
        return self.available

    def stop(self) -> None:
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)

    def _callback(self, proxy, etype, event, refcon):
        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            if self._tap is not None:
                Quartz.CGEventTapEnable(self._tap, True)
            return event
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData) == MAGIC:
            return event
        self._last = time.monotonic()
        if etype == Quartz.kCGEventKeyDown:
            flags = Quartz.CGEventGetFlags(event)
            if flags & _HOTKEY_FLAGS == _HOTKEY_FLAGS:
                code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                if code == _KEY_P:
                    self.paused_by_hotkey = not self.paused_by_hotkey
                elif code == _KEY_X:
                    self.abort_requested = True
        return event

    def _run(self) -> None:
        mask = 0
        for et in (
            Quartz.kCGEventKeyDown,
            Quartz.kCGEventFlagsChanged,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventRightMouseDown,
            Quartz.kCGEventOtherMouseDown,
        ):
            mask |= Quartz.CGEventMaskBit(et)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if self._tap is None:
            self._ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self.available = True
        self._ready.set()
        Quartz.CFRunLoopRun()
