"""Typing engine: turns a target file into keystrokes that survive IDE smart keys.

JetBrains editors auto-pair brackets/quotes, auto-indent, add a closing ``}``
on Enter, open completion popups, and auto-close XML tags. Typing a file
character-by-character naively therefore corrupts it. The engine keeps one
invariant instead: **nothing exists after the caret**. Per line it

  1. types the line's text (without its indentation - the IDE indents),
  2. presses Escape (closes completion popups so Enter can't accept one),
  3. selects to document end and deletes (drops auto-inserted closers),
  4. presses Enter, then again selects to document end and deletes
     (drops the ``}`` / doc-comment tail the IDE adds on Enter).

Whenever typing pauses (you left the IDE, clicked, or typed), the engine
re-syncs: saves, reads the file from disk, keeps the longest correct prefix
of lines, deletes the rest and continues from there. At the end it verifies
the saved file against the target (whitespace-insensitive) and, if anything
still differs, pastes the exact target as a repair.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings
from .backend import Clipboard, FileOpener, Keyboard, parse_chord
from .gate import FocusGate

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Whitespace-insensitive form used for verification."""
    return _WS.sub("", text)


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.rstrip("\n").split("\n")]


@dataclass
class FileOutcome:
    path: str
    status: str  # "typed" | "repaired" | "mismatch" | "unchanged"
    lines: int
    resyncs: int


def xml_skip_quote_positions(target: str) -> set[int]:
    """Indices (in ``target``) of opening attribute quotes right after '=' inside a tag.

    Android Studio inserts ``""`` when you type ``=`` in a tag, so typing the
    opening quote ourselves would double it.
    """
    skip: set[int] = set()
    in_tag = False
    in_comment = False
    special = False
    quote: str | None = None
    i = 0
    n = len(target)
    while i < n:
        c = target[i]
        if in_comment:
            if target.startswith("-->", i):
                in_comment = False
                i += 3
                continue
        elif quote:
            if c == quote:
                quote = None
        elif target.startswith("<!--", i):
            in_comment = True
            i += 4
            continue
        elif c == "<":
            in_tag = True
            special = target[i + 1 : i + 2] in ("?", "!")  # prolog / doctype: no auto-quotes
        elif c == ">":
            in_tag = False
        elif in_tag and c in "\"'":
            if c == '"' and i > 0 and target[i - 1] == "=" and not special:
                skip.add(i)
            quote = c
        i += 1
    return skip


class TypingEngine:
    def __init__(
        self,
        keyboard: Keyboard,
        clipboard: Clipboard,
        opener: FileOpener,
        gate: FocusGate,
        settings: Settings,
        on_progress: Callable[[str, int, int], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        open_wait: float = 1.0,
        save_wait: float = 0.4,
    ):
        self.kb = keyboard
        self.clipboard = clipboard
        self.opener = opener
        self.gate = gate
        self.s = settings
        self.on_progress = on_progress or (lambda path, line, total: None)
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.open_wait = open_wait
        self.save_wait = save_wait
        self._path: str | None = None
        self._dirty = False  # a pause happened -> re-sync before typing more

    # ------------------------------------------------------------------ keys
    def _ready(self, check_title: bool = True) -> None:
        if self.gate.wait_ready(self._path, check_title=check_title):
            self._dirty = True

    def _chord(self, chord: str) -> None:
        self._ready(check_title=False)
        key, mods = parse_chord(chord)
        self.kb.press(key, mods)
        self.gate.mark_sent()
        self.sleep(self.s.key_gap)

    def _cmd(self, name: str) -> None:
        self._chord(getattr(self.s.keymap, name))

    def _char(self, ch: str, delay: bool = True) -> None:
        self.kb.type_char(ch)
        self.gate.mark_sent()
        if delay:
            base = self.s.char_delay()
            j = self.s.jitter
            self.sleep(max(0.0, base * self.rng.uniform(1 - j, 1 + j)))

    def _clear_after_caret(self) -> None:
        self._cmd("select_to_doc_end")
        self._cmd("delete_forward")

    def _newline(self) -> None:
        self._cmd("escape")
        self._clear_after_caret()
        self._cmd("newline")
        self._clear_after_caret()

    def _goto_line(self, line: int) -> None:
        self._cmd("goto_line")
        self.sleep(0.35)  # dialog opens
        for ch in f"{line}:1":
            self._char(ch, delay=False)
            self.sleep(0.01)
        self._cmd("newline")
        self.sleep(0.15)

    def _save(self) -> None:
        self._cmd("save")
        self.sleep(self.save_wait)

    # ------------------------------------------------------------------ sync
    def _read_disk(self) -> str:
        assert self._path
        try:
            return Path(self._path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _sync(self, body: list[str]) -> tuple[int, bool]:
        """Put the caret on an empty last line after the longest correct prefix.

        Returns (index of next target line to type, whether that line must be
        typed with its own indentation because the IDE did not indent it).
        """
        for _ in range(6):
            self._dirty = False
            self._save()
            disk = self._read_disk().split("\n")
            k = 0
            while k < len(body) and k < len(disk) and disk[k].strip() == body[k].strip():
                k += 1
            # A line is only "done" if it is followed by a newline in the doc.
            if k == len(disk):
                k -= 1
            if k <= 0:
                self._cmd("select_all")
                self._cmd("delete_forward")
                k, own_indent = 0, True
            else:
                self._goto_line(k + 1)
                self._clear_after_caret()
                own_indent = True
            if not self._dirty:
                return k, own_indent
        return k, own_indent

    # ------------------------------------------------------------------ files
    def open(self, path: str) -> None:
        self._path = path
        p = Path(path)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
        self._ready(check_title=False)
        self.opener.open_file(path)
        self.sleep(self.open_wait)
        self._ready(check_title=True)

    def type_file(self, path: str, target: str) -> FileOutcome:
        target = target.replace("\r\n", "\n").replace("\t", "    ")
        target = target.rstrip("\n") + "\n"
        body = target[:-1].split("\n")
        self.open(path)

        if normalize(self._read_disk()) == normalize(target):
            return FileOutcome(path, "unchanged", len(body), 0)

        if self.s.instant:
            return self._paste_file(path, target, len(body), resyncs=0, status="typed")

        is_xml = path.endswith(".xml") and self.s.xml_attr_quotes
        skip = xml_skip_quote_positions(target) if is_xml else set()
        line_offsets = []
        off = 0
        for line in body:
            line_offsets.append(off)
            off += len(line) + 1

        resyncs = 0
        i, own_indent = self._sync(body)
        while i < len(body):
            line = body[i]
            self.on_progress(path, i + 1, len(body))
            start = 0 if own_indent else len(line) - len(line.lstrip(" "))
            restarted = False
            for col in range(start, len(line)):
                self._ready(check_title=True)
                if self._dirty:
                    restarted = True
                    break
                if line_offsets[i] + col in skip:
                    continue
                self._char(line[col])
            if not restarted:
                self._newline()
                own_indent = False
                if self.s.line_pause:
                    self.sleep(self.s.line_pause * self.rng.uniform(0.5, 1.5))
                i += 1
            if self._dirty or restarted:
                resyncs += 1
                i, own_indent = self._sync(body)

        if self.s.reformat:
            self._cmd("reformat")
            self.sleep(0.5)
        self._save()
        disk = self._read_disk()
        if normalize(disk) == normalize(target):
            if self.s.exact_whitespace and _lines(disk) != _lines(target):
                # Same code, IDE indented it differently: snap to the exact target.
                return self._paste_file(path, target, len(body), resyncs, status="typed")
            return FileOutcome(path, "typed", len(body), resyncs)
        if not self.s.repair:
            return FileOutcome(path, "mismatch", len(body), resyncs)
        return self._paste_file(path, target, len(body), resyncs, status="repaired")

    def _paste_file(self, path: str, target: str, lines: int, resyncs: int, status: str) -> FileOutcome:
        snapshot = getattr(self.clipboard, "snapshot", None)
        saved = snapshot() if snapshot else self.clipboard.get_text()
        try:
            self.clipboard.set_text(target, transient=True)
            self._cmd("select_all")
            self._cmd("paste")
            self.sleep(0.5)
            self._save()
        finally:
            restore = getattr(self.clipboard, "restore", None)
            if restore:
                restore(saved)
            elif saved is not None:
                self.clipboard.set_text(saved, transient=False)
            else:
                self.clipboard.set_text("", transient=True)  # don't leave the solution on the clipboard
        ok = normalize(self._read_disk()) == normalize(target)
        return FileOutcome(path, status if ok else "mismatch", lines, resyncs)
