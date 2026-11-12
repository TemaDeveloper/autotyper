"""Interfaces between the typing engine and the outside world.

The engine never talks to macOS directly. It drives three small protocols:

* ``Keyboard``  - posts keystrokes into whatever app is frontmost.
* ``Clipboard`` - read/write the system pasteboard (used only for repair).
* ``FileOpener`` - asks the IDE to open/focus a file.

Real implementations live in ``autotyper.platform.macos``; tests use
``autotyper.typer.fake_editor.FakeJetBrainsEditor`` which implements all three
and simulates JetBrains "smart keys" behaviour.

Key names (``Keyboard.press``):
    "return", "escape", "tab", "delete" (backspace), "forward_delete",
    "home", "end", "left", "right", "up", "down", "page_up", "page_down",
    "space", single lowercase letters "a".."z", digits "0".."9".
Modifier names: "cmd", "shift", "alt", "ctrl".
"""

from __future__ import annotations

from typing import Protocol, Sequence


class Keyboard(Protocol):
    def type_char(self, ch: str) -> None:
        """Type exactly one printable character (never "\\n" or "\\t")."""

    def press(self, key: str, mods: Sequence[str] = ()) -> None:
        """Press and release a named key with optional modifiers."""


class Clipboard(Protocol):
    def get_text(self) -> str | None: ...

    def set_text(self, text: str, transient: bool = True) -> None:
        """Set clipboard text. ``transient`` asks clipboard managers to ignore it."""


class FileOpener(Protocol):
    def open_file(self, path: str) -> None:
        """Ask the IDE to open ``path`` (absolute) in an editor tab and focus it."""


def parse_chord(chord: str) -> tuple[str, tuple[str, ...]]:
    """"cmd+shift+end" -> ("end", ("cmd", "shift"))."""
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty chord: {chord!r}")
    return parts[-1], tuple(parts[:-1])
