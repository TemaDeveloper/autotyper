"""Floating status overlay (HUD) shown while autotyper types.

A small translucent panel in the top-right corner that shows the current
file, progress and whether typing is running or paused (and why). It never
takes focus or mouse clicks, follows you across desktops (Spaces), and is
an ordinary window as far as screenshots and screen sharing are concerned.

It runs in its own process (`python -m autotyper.platform.hud`) because
AppKit needs the main thread; the CLI sends it JSON lines on stdin:

    {"state": "typing", "msg": "...", "file": "A.kt", "index": 1, "count": 3, "line": 10, "total": 64}

The process exits when stdin closes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass

STATES = {
    # state: (icon, title, rgb)
    "typing": ("●", "Typing", (0.30, 0.85, 0.45)),
    "waiting": ("❚❚", "Paused", (1.00, 0.78, 0.25)),
    "info": ("●", "Working", (0.40, 0.70, 1.00)),
    "done": ("✔", "Done", (0.30, 0.85, 0.45)),
    "stopped": ("■", "Stopped", (1.00, 0.40, 0.40)),
}
HOTKEYS = "⌃⌥⌘P pause/resume   ⌃⌥⌘X stop"


@dataclass
class HudView:
    title: str = "autotyper"
    rgb: tuple[float, float, float] = (0.40, 0.70, 1.00)
    detail: str = "Starting…"
    file_line: str = ""
    fraction: float = 0.0


class HudModel:
    """Turns status messages into what the panel shows. No AppKit here, so it is testable."""

    def __init__(self) -> None:
        self.state = "info"
        self.msg = "Starting…"
        self.file = ""
        self.index = 0
        self.count = 0
        self.line = 0
        self.total = 0

    def apply(self, data: dict) -> HudView:
        for key in ("state", "msg", "file", "index", "count", "line", "total"):
            if key in data and data[key] is not None:
                setattr(self, key, data[key])
        return self.view()

    def view(self) -> HudView:
        icon, title, rgb = STATES.get(self.state, STATES["info"])
        file_line = ""
        if self.file:
            file_line = self.file
            if self.count:
                file_line += f"  ·  file {self.index}/{self.count}"
            if self.total:
                file_line += f"  ·  line {min(self.line, self.total)}/{self.total}"
        done_files = max(self.index - 1, 0)
        within = (self.line / self.total) if self.total else 0.0
        fraction = 1.0 if self.state == "done" else ((done_files + within) / self.count if self.count else within)
        return HudView(f"{icon}  {title}", rgb, self.msg, file_line, max(0.0, min(1.0, fraction)))


# ---------------------------------------------------------------------- client (CLI side)
class HudClient:
    """Starts the overlay process and sends it updates. Every failure is silent: the HUD is optional."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "autotyper.platform.hud"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
            )
        except OSError:
            self.proc = None

    def update(self, **fields) -> None:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write(json.dumps(fields) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.proc = None

    def close(self, linger: float = 0.0) -> None:
        if not self.proc:
            return
        try:
            if linger:
                self.update(linger=linger)
            self.proc.stdin.close()
            self.proc.wait(timeout=linger + 3)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.proc.kill()
        self.proc = None


# ---------------------------------------------------------------------- overlay (its own process)
def run() -> None:  # pragma: no cover - needs a GUI session
    import AppKit
    from PyObjCTools import AppHelper

    W, H, MARGIN = 360, 104, 16
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon, never activates

    screen = AppKit.NSScreen.mainScreen().visibleFrame()
    frame = AppKit.NSMakeRect(
        screen.origin.x + screen.size.width - W - MARGIN,
        screen.origin.y + screen.size.height - H - MARGIN, W, H,
    )
    panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame,
        AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
        AppKit.NSBackingStoreBuffered, False,
    )
    panel.setLevel_(AppKit.NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(AppKit.NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setIgnoresMouseEvents_(True)  # clicks go through to the window underneath
    panel.setHidesOnDeactivate_(False)
    panel.setSharingType_(AppKit.NSWindowSharingReadOnly)  # normal window: visible in screenshots and shares
    panel.setCollectionBehavior_(
        AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
        | AppKit.NSWindowCollectionBehaviorStationary
        | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        | AppKit.NSWindowCollectionBehaviorIgnoresCycle
    )

    bg = AppKit.NSVisualEffectView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
    bg.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
    bg.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
    bg.setState_(AppKit.NSVisualEffectStateActive)
    bg.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameVibrantDark))
    bg.setWantsLayer_(True)
    bg.layer().setCornerRadius_(14.0)
    bg.layer().setMasksToBounds_(True)
    panel.setContentView_(bg)

    def label(y: float, size: float, bold: bool = False, alpha: float = 1.0):
        f = AppKit.NSTextField.labelWithString_("")
        f.setFrame_(AppKit.NSMakeRect(16, y, W - 32, size + 6))
        font = AppKit.NSFont.boldSystemFontOfSize_(size) if bold else AppKit.NSFont.systemFontOfSize_(size)
        f.setFont_(font)
        f.setTextColor_(AppKit.NSColor.whiteColor().colorWithAlphaComponent_(alpha))
        f.setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
        bg.addSubview_(f)
        return f

    title = label(H - 30, 14, bold=True)
    brand = label(H - 30, 11, alpha=0.45)
    brand.setAlignment_(AppKit.NSTextAlignmentRight)
    brand.setStringValue_("autotyper")
    detail = label(H - 50, 12, alpha=0.9)
    file_line = label(H - 68, 11, alpha=0.65)
    bar = AppKit.NSProgressIndicator.alloc().initWithFrame_(AppKit.NSMakeRect(16, 22, W - 32, 8))
    bar.setStyle_(AppKit.NSProgressIndicatorStyleBar)
    bar.setIndeterminate_(False)
    bar.setMinValue_(0.0)
    bar.setMaxValue_(1.0)
    bg.addSubview_(bar)
    keys = label(4, 10, alpha=0.45)
    keys.setStringValue_(HOTKEYS)

    model = HudModel()

    def render(v: HudView) -> None:
        title.setStringValue_(v.title)
        title.setTextColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*v.rgb, 1.0))
        detail.setStringValue_(v.detail)
        file_line.setStringValue_(v.file_line)
        bar.setDoubleValue_(v.fraction)

    render(model.view())
    panel.orderFrontRegardless()

    def reader() -> None:
        linger = 0.0
        for raw in sys.stdin:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            linger = float(data.get("linger", linger) or 0.0)
            AppHelper.callAfter(render, model.apply(data))
        AppHelper.callLater(linger, app.terminate_, None)

    threading.Thread(target=reader, daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=True)


def demo() -> None:  # pragma: no cover - visual preview: python -m autotyper.platform.hud --demo
    import time

    hud = HudClient()
    time.sleep(1.5)
    for i in range(1, 41):
        hud.update(state="typing", msg="Typing into Android Studio", file="SettingsScreen.kt",
                   index=1, count=2, line=i, total=40)
        time.sleep(0.08)
    hud.update(state="waiting", msg="Firefox is in front - come back to Android Studio")
    time.sleep(2.5)
    hud.update(state="info", msg="Back in Android Studio - checking the file")
    time.sleep(1.0)
    for i in range(1, 31):
        hud.update(state="typing", msg="Typing into Android Studio", file="MainActivity.kt",
                   index=2, count=2, line=i, total=30)
        time.sleep(0.08)
    hud.update(state="done", msg="2 file(s) written")
    hud.close(linger=2.5)


if __name__ == "__main__":  # pragma: no cover
    demo() if "--demo" in sys.argv else run()
