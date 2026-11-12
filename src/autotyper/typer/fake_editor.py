"""In-memory simulator of a JetBrains (Android Studio) code editor.

``FakeJetBrainsEditor`` implements the ``Keyboard``, ``Clipboard`` and
``FileOpener`` protocols from :mod:`autotyper.typer.backend`. It reacts to
keystrokes the way the IDE's "smart keys" do (bracket and quote auto-pairing,
auto-indent, closing-brace insertion, doc comment stubs, XML tag helpers,
completion popup), so the typing engine can be tested against realistic
editor behaviour without macOS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

CLOSERS: dict[str, str] = {"(": ")", "[": "]", "{": "}"}
OPENERS: tuple[str, ...] = ("{", "(", "[")
OVERTYPE_CHARS = ')]}"\'>'
COMPLETION_TEXT = "/*completion*/"

_IDENT = re.compile(r"[A-Za-z0-9_]")
_TAG_NAME = re.compile(r"[A-Za-z_][\w.:-]*")


@dataclass
class SmartKeys:
    pair_brackets: bool = True
    pair_quotes: bool = True
    smart_indent: bool = True
    enter_closes_brace: bool = True
    doc_comment_stub: bool = True
    xml_close_tag: bool = True
    xml_attr_quotes: bool = True
    xml_self_close: bool = True
    completion_popup: bool = True


class FakeJetBrainsEditor:
    """Keyboard, Clipboard and FileOpener backed by an in-memory document."""

    def __init__(self, smart: SmartKeys | None = None, indent: int = 4) -> None:
        self.smart: SmartKeys = smart if smart is not None else SmartKeys()
        self.indent: int = indent
        self.path: str | None = None
        self.text: str = ""
        self.caret: int = 0
        self.anchor: int | None = None
        self.popup_open: bool = False
        self.goto_buffer: str | None = None
        self.clipboard: str | None = None
        self.events: list[tuple] = []
        self.auto_closers: set[int] = set()

    # ------------------------------------------------------------------
    # Keyboard protocol
    # ------------------------------------------------------------------
    def type_char(self, ch: str) -> None:
        self.events.append(("type_char", ch))
        if self.goto_buffer is not None:
            self.goto_buffer += ch
            return

        self._delete_selection()
        if self._is_overtype(ch):
            self.auto_closers.discard(self.caret)
            self.caret += 1
        else:
            self._insert(self.caret, ch)
            self.caret += 1
            self._apply_smart_keys(ch)

        self.popup_open = bool(self.smart.completion_popup and _IDENT.fullmatch(ch))

    def press(self, key: str, mods: Sequence[str] = ()) -> None:
        mods_t = tuple(mods)
        m = set(mods_t)
        self.events.append(("press", key, mods_t))

        # Every non-typing key closes the popup; Enter reads the old state first.
        popup = self.popup_open
        self.popup_open = False

        if key == "escape" and not m:
            self.goto_buffer = None
            self.anchor = None
            return

        if self.goto_buffer is not None:
            # Go to Line dialog is modal: only Enter, Delete and Escape matter.
            if key == "return" and not m:
                self._goto(self.goto_buffer)
                self.goto_buffer = None
            elif key == "delete" and not m:
                self.goto_buffer = self.goto_buffer[:-1]
            return

        if key == "return" and not m:
            if popup:
                self._delete_selection()
                self._insert(self.caret, COMPLETION_TEXT)
                self.caret += len(COMPLETION_TEXT)
            else:
                self._newline()
            return

        if key == "forward_delete" and not m:
            if not self._delete_selection() and self.caret < len(self.text):
                self._delete(self.caret, self.caret + 1)
            return

        if key == "delete" and not m:
            if not self._delete_selection() and self.caret > 0:
                self._delete(self.caret - 1, self.caret)
                self.caret -= 1
            return

        if key == "a" and m == {"cmd"}:
            self.anchor = 0
            self.caret = len(self.text)
            return

        if key == "l" and m == {"cmd"}:
            self.goto_buffer = ""
            return

        if key == "l" and m == {"cmd", "alt"}:
            self._reformat()
            return

        if key == "s" and m == {"cmd"}:
            self._save()
            return

        if key == "v" and m == {"cmd"}:
            self._delete_selection()
            if self.clipboard:
                self._insert(self.caret, self.clipboard)
                self.caret += len(self.clipboard)
            return

        if key in ("home", "end") and m <= {"cmd", "shift"}:
            if key == "home":
                target = 0 if "cmd" in m else self._line_start(self.caret)
            else:
                target = len(self.text) if "cmd" in m else self._line_end(self.caret)
            self._move(target, extend="shift" in m)
            return

        if key in ("left", "right") and m in (set(), {"shift"}):
            step = -1 if key == "left" else 1
            target = min(max(self.caret + step, 0), len(self.text))
            self._move(target, extend="shift" in m)
            return

        if key in ("up", "down") and m in (set(), {"shift"}):
            self._move(self._vertical_target(-1 if key == "up" else 1), extend="shift" in m)
            return

        # Anything else was recorded in ``events`` and is ignored.

    # ------------------------------------------------------------------
    # Clipboard protocol
    # ------------------------------------------------------------------
    def get_text(self) -> str | None:
        self.events.append(("get_text",))
        return self.clipboard

    def set_text(self, text: str, transient: bool = True) -> None:
        self.events.append(("set_text", text, transient))
        self.clipboard = text

    # ------------------------------------------------------------------
    # FileOpener protocol
    # ------------------------------------------------------------------
    def open_file(self, path: str) -> None:
        self.events.append(("open_file", path))
        if self.path is not None:
            self._save()  # JetBrains autosaves the previous tab on switch.

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("", encoding="utf-8")

        self.path = path
        self.text = target.read_text(encoding="utf-8")
        self.caret = 0
        self.anchor = None
        self.popup_open = False
        self.goto_buffer = None
        self.auto_closers = set()

    # ------------------------------------------------------------------
    # Document primitives: every edit goes through these two so that
    # auto_closers indices stay valid.
    # ------------------------------------------------------------------
    def _insert(self, pos: int, s: str, auto: bool = False) -> None:
        if not s:
            return
        n = len(s)
        self.text = self.text[:pos] + s + self.text[pos:]
        shifted = {i + n if i >= pos else i for i in self.auto_closers}
        if auto:
            shifted.update(range(pos, pos + n))
        self.auto_closers = shifted

    def _delete(self, start: int, end: int) -> None:
        n = end - start
        if n <= 0:
            return
        self.text = self.text[:start] + self.text[end:]
        kept: set[int] = set()
        for i in self.auto_closers:
            if i < start:
                kept.add(i)
            elif i >= end:
                kept.add(i - n)
        self.auto_closers = kept

    def _delete_selection(self) -> bool:
        if self.anchor is None:
            return False
        if self.anchor == self.caret:
            self.anchor = None
            return False
        start, end = sorted((self.anchor, self.caret))
        self._delete(start, end)
        self.caret = start
        self.anchor = None
        return True

    # ------------------------------------------------------------------
    # Typing helpers
    # ------------------------------------------------------------------
    def _is_overtype(self, ch: str) -> bool:
        return (
            ch in OVERTYPE_CHARS
            and self.caret < len(self.text)
            and self.text[self.caret] == ch
            and self.caret in self.auto_closers
        )

    def _apply_smart_keys(self, ch: str) -> None:
        """Called right after ``ch`` was inserted; the caret sits after it."""
        sk = self.smart
        pos = self.caret - 1  # index of the character just typed

        if sk.pair_brackets and ch in CLOSERS:
            self._insert(self.caret, CLOSERS[ch], auto=True)
        elif sk.pair_quotes and ch in "\"'":
            prev = self.text[pos - 1] if pos > 0 else ""
            if not (_IDENT.fullmatch(prev) or prev == "\\"):
                self._insert(self.caret, ch, auto=True)
        elif sk.smart_indent and ch == "}":
            self._dedent_before(pos)

        if self.path and self.path.endswith(".xml"):
            self._xml_rules(ch, pos)

    def _dedent_before(self, pos: int) -> None:
        """Remove up to ``indent`` leading spaces when ``}`` lands on a whitespace-only line."""
        line_start = self._line_start(pos)
        ws = self.text[line_start:pos]
        if ws and not ws.strip():
            n = min(self.indent, len(ws))
            self._delete(pos - n, pos)
            self.caret -= n

    def _xml_rules(self, ch: str, pos: int) -> None:
        sk = self.smart
        if ch == "=":
            if sk.xml_attr_quotes and self._open_start_tag() is not None:
                self._insert(self.caret, '"')
                self._insert(self.caret + 1, '"', auto=True)
                self.caret += 1
        elif ch == "/":
            if (
                sk.xml_self_close
                and self._open_start_tag() is not None
                and self.text[self.caret : self.caret + 1] != ">"
            ):
                self._insert(self.caret, ">", auto=True)
        elif ch == ">" and sk.xml_close_tag:
            lt = self.text.rfind("<", 0, pos)
            if lt < 0 or ">" in self.text[lt:pos] or pos - 1 <= lt:
                return
            if self.text[pos - 1] == "/":
                return
            name = _TAG_NAME.match(self.text, lt + 1)
            if name is not None:
                self._insert(self.caret, f"</{name.group(0)}>", auto=True)

    def _open_start_tag(self) -> int | None:
        """Index of the '<' of the start tag the caret is inside, or None."""
        lt = self.text.rfind("<", 0, self.caret)
        if lt < 0 or ">" in self.text[lt : self.caret]:
            return None
        if self.text[lt + 1 : lt + 2] in ("/", "?", "!"):
            return None
        return lt

    def _newline(self) -> None:
        self._delete_selection()
        pos = self.caret
        line_start = self._line_start(pos)
        line = self.text[line_start : self._line_end(pos)]
        cur_indent = line[: len(line) - len(line.lstrip(" \t"))]
        before = self.text[line_start:pos]
        last = before.rstrip()[-1:] or ""
        sk = self.smart

        if sk.doc_comment_stub and before.strip().endswith("/**"):
            first = "\n" + cur_indent + " * "
            self._insert(pos, first)
            self.caret = pos + len(first)
            self._insert(self.caret, "\n" + cur_indent + " */")
            return

        if not sk.smart_indent:
            new_indent = ""
        elif last in OPENERS:
            new_indent = cur_indent + " " * self.indent
        else:
            new_indent = cur_indent

        ins = "\n" + new_indent
        self._insert(pos, ins)
        self.caret = pos + len(ins)

        next_char = self.text[self.caret : self.caret + 1]
        if last in OPENERS and next_char == CLOSERS[last]:
            # Caret was between a fresh pair: push the closer onto its own line.
            self._insert(self.caret, "\n" + cur_indent)
        elif sk.enter_closes_brace and last == "{" and self.text.count("{") > self.text.count("}"):
            self._insert(self.caret, "\n" + cur_indent + "}")

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------
    def _move(self, target: int, extend: bool) -> None:
        if extend:
            if self.anchor is None:
                self.anchor = self.caret
        else:
            self.anchor = None
        self.caret = target
        if self.anchor == self.caret:
            self.anchor = None

    def _line_start(self, pos: int) -> int:
        return self.text.rfind("\n", 0, pos) + 1

    def _line_end(self, pos: int) -> int:
        end = self.text.find("\n", pos)
        return len(self.text) if end < 0 else end

    def _vertical_target(self, delta: int) -> int:
        lines = self.text.split("\n")
        line_idx = self.text.count("\n", 0, self.caret)
        col = self.caret - self._line_start(self.caret)
        target_idx = line_idx + delta
        if not 0 <= target_idx < len(lines):
            return 0 if delta < 0 else len(self.text)
        start = sum(len(line) + 1 for line in lines[:target_idx])
        return start + min(col, len(lines[target_idx]))

    def _goto(self, spec: str) -> None:
        head, _, tail = spec.partition(":")
        try:
            line = int(head.strip())
            col = int(tail.strip()) if tail.strip() else 1
        except ValueError:
            return
        lines = self.text.split("\n")
        line = min(max(line, 1), len(lines))
        start = sum(len(text_line) + 1 for text_line in lines[: line - 1])
        length = len(lines[line - 1])
        col = min(max(col, 1), length + 1)
        self.caret = start + col - 1
        self.anchor = None

    # ------------------------------------------------------------------
    # File and formatting helpers
    # ------------------------------------------------------------------
    def _save(self) -> None:
        if self.path:
            Path(self.path).write_text(self.text, encoding="utf-8")

    def _reformat(self) -> None:
        out: list[str] = []
        depth = 0
        for raw in self.text.split("\n"):
            stripped = raw.strip()
            if not stripped:
                out.append("")
                continue
            leading_closers = len(stripped) - len(stripped.lstrip(")]}"))
            level = max(0, depth - leading_closers)
            out.append(" " * (self.indent * level) + stripped)
            for ch in stripped:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth = max(0, depth - 1)
        self.text = "\n".join(out)
        self.caret = min(self.caret, len(self.text))
        self.anchor = None
        self.auto_closers = set()
