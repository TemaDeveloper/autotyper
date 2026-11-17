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

import math
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

_QWERTY_ROWS = ("1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm")

_KOTLIN_DECL = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|override|suspend|open|abstract|final|inline|tailrec|data|sealed|enum|annotation|value|lateinit|const)\s+)*"
    r"(fun|class|interface|object|init|companion object)\b"
)
_OPEN_TAG = re.compile(r"<\s*([A-Za-z_][\w.:-]*)")

# Characters after which a person naturally hesitates a beat while coding.
_BOUNDARY_CHARS = {",", ";", "{", "}"}

# Distinct typing gears. Runs of 1-3 keys share a gear, then switch to a
# different one, so adjacent keys are never stuck at the same speed.
_TEMPOS = (0.5, 0.75, 1.0, 1.4, 1.9)


def plausible_typo(rng: random.Random, ch: str) -> str | None:
    """Return a nearby QWERTY key for ``ch`` (a plausible miskey), or None.

    Only letters and digits get a plausible neighbour - punctuation, quotes
    and brackets are left alone because the IDE treats those specially.
    """
    if not ch.isascii() or not ch.isalnum():
        return None
    low = ch.lower()
    for row in _QWERTY_ROWS:
        i = row.find(low)
        if i < 0:
            continue
        neighbours = []
        if i > 0:
            neighbours.append(row[i - 1])
        if i < len(row) - 1:
            neighbours.append(row[i + 1])
        wrong = rng.choice(neighbours)
        return wrong.upper() if ch.isupper() else wrong
    return None


def _brace_span(body: list[str], start: int) -> int:
    """Line index of the ``}`` that closes the block starting at ``start``."""
    depth = 0
    saw_open = False
    for j in range(start, len(body)):
        for ch in body[j]:
            if ch == "{":
                depth += 1
                saw_open = True
            elif ch == "}":
                depth -= 1
        if saw_open and depth <= 0:
            return j
    return len(body) - 1 if saw_open else start


def _xml_span(body: list[str], start: int) -> int:
    """Line index where the XML element starting at ``start`` closes.

    Scans tags character by character so opening tags that spread their
    attributes over several lines (and end with ``/>``) are handled.
    """
    depth = 0
    in_comment = False
    in_tag = False
    closing = False
    self_closing = False
    for j in range(start, len(body)):
        line = body[j]
        k = 0
        while k < len(line):
            if in_comment:
                if line.startswith("-->", k):
                    in_comment = False
                    k += 3
                else:
                    k += 1
                continue

            if in_tag:
                c = line[k]
                if c == "/" and k + 1 < len(line) and line[k + 1] == ">":
                    self_closing = True
                elif c == ">":
                    if closing or self_closing:
                        depth -= 1
                    in_tag = False
                    closing = False
                    self_closing = False
                k += 1
                continue

            if line.startswith("<!--", k):
                in_comment = True
                k += 4
                continue

            c = line[k]
            if c == "<":
                if line.startswith("</", k):
                    in_tag = True
                    closing = True
                    self_closing = False
                    k += 2
                elif line.startswith("<?", k) or line.startswith("<!", k):
                    in_tag = True
                    closing = False
                    self_closing = False
                    k += 2
                else:
                    depth += 1  # opening tag; self-closing cancels this at '>'
                    in_tag = True
                    closing = False
                    self_closing = False
                    k += 1
                continue
            k += 1

        if depth <= 0 and j > start:
            return j
    return len(body) - 1


def block_starts(body: list[str], is_xml: bool) -> dict[int, list[int]]:
    """Map ``end_line -> [start_lines]`` of multi-line method/class/XML blocks.

    Only multi-line blocks are returned; single lines fall back to the
    ordinary line-rewrite mistake. A slightly imperfect span is harmless: the
    block is always retyped from the exact target lines, so the file still ends
    up correct.
    """
    ends: dict[int, list[int]] = {}
    if is_xml:
        for start, line in enumerate(body):
            stripped = line.lstrip()
            if not (stripped.startswith("<") and not stripped.startswith(("</", "<!--", "<?", "<!"))):
                continue
            m = _OPEN_TAG.match(stripped)
            if m is None:
                continue
            # Skip tags that close on this same line (self-closing or <a></a>).
            if "/>" in stripped or f"</{m.group(1)}" in stripped:
                continue
            end = _xml_span(body, start)
            if end > start:
                ends.setdefault(end, []).append(start)
    else:
        for start, line in enumerate(body):
            if _KOTLIN_DECL.match(line):
                end = _brace_span(body, start)
                if end > start:
                    ends.setdefault(end, []).append(start)
    return ends


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
    mistakes: int = 0


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
        # Human pacing state: each character picks a short "tempo run" so the
        # gap before every key keeps changing - a couple of fast keys, then one
        # slow one, then some at a middle pace, never a steady metronome.
        self._tempo: float | None = None
        self._tempo_left = 0

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

    def _key(self, key: str, mods: tuple[str, ...] = ()) -> None:
        self._ready(check_title=False)
        self.kb.press(key, mods)
        self.gate.mark_sent()
        self.sleep(self.s.key_gap)

    def _char(self, ch: str, delay: bool = True) -> None:
        self.kb.type_char(ch)
        self.gate.mark_sent()
        if delay:
            self.sleep(self._human_delay())

    def _human_delay(self) -> float:
        """A delay that changes from key to key, like a person's typing.

        A "tempo run" of 1-3 keys all share a rough gear, but every run picks
        a *different* gear (very fast, fast, medium, slow, very slow), so the
        next key is never the same speed as the one before for long. On top of
        that each key gets its own small log-normal jitter.
        """
        if self._tempo_left <= 0:
            old = self._tempo
            choices = [t for t in _TEMPOS if old is None or t != old]
            self._tempo = self.rng.choice(choices)
            self._tempo_left = self.rng.randint(1, 3)
        self._tempo_left -= 1
        sigma = max(0.04, self.s.jitter * 0.4)
        factor = math.exp(self.rng.gauss(0.0, sigma))
        return max(0.012, self.s.char_delay() * self._tempo * factor)

    def _pause(self, mean: float, sigma: float = 0.6) -> None:
        """Pause for a log-normally distributed length around ``mean`` seconds."""
        if mean > 0:
            self.sleep(max(0.0, mean * math.exp(self.rng.gauss(0.0, sigma))))

    def _human_char(self, ch: str) -> None:
        """Type ``ch`` the way a person would: occasional miskey, then fix it."""
        if self.s.typo_rate > 0:
            wrong = plausible_typo(self.rng, ch)
            if wrong is not None and wrong != ch and self.rng.random() < self.s.typo_rate:
                self._char(wrong)
                # A beat to notice the mistake, then backspace and retype.
                self._pause(self.s.char_delay() * 0.8, 0.7)
                self._key("delete")
        self._char(ch)
        if self.s.hesitation > 0 and self.rng.random() < self.s.hesitation:
            self._pause(self.s.hesitation_pause, 0.8)
        if ch == " " and self.s.word_pause:
            self._pause(self.s.word_pause, 0.7)
        elif ch in _BOUNDARY_CHARS:
            self._pause(self.s.char_delay() * 0.9, 0.8)

    def _format_slip(self, indent: int, own_indent: bool) -> int:
        """Over-indent the line by one level, pause, then delete the extra spaces.

        Leaves the caret where the line's content should start and returns the
        column to start typing from (the correct indentation).
        """
        extra = 4
        if not own_indent:
            self._key("home")  # line start, before the IDE's auto-indent
        for _ in range((indent if own_indent else 0) + extra):
            self.kb.type_char(" ")
            self.gate.mark_sent()
            self.sleep(self.s.key_gap)
        self._pause(self.s.char_delay() * 0.9, 0.7)
        for _ in range(extra):
            self._key("delete")
        if not own_indent:
            self._key("end")  # back to after the auto-indent
        return indent

    def _delete_last_line(self) -> None:
        """Delete the line above the caret (the one just typed), landing on it."""
        self._key("up")
        self._key("home")
        self._key("end", ("shift",))
        self._key("forward_delete")

    def _delete_block(self, start_line: int) -> None:
        """Delete everything from ``start_line`` to the end of the document."""
        self._goto_line(start_line + 1)
        self._clear_after_caret()

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
            return self._paste_file(path, target, len(body), resyncs=0, status="typed", mistakes=0)

        is_xml_file = path.endswith(".xml")
        skip = xml_skip_quote_positions(target) if (is_xml_file and self.s.xml_attr_quotes) else set()
        line_offsets = []
        off = 0
        for line in body:
            line_offsets.append(off)
            off += len(line) + 1

        block_ends = block_starts(body, is_xml_file)
        resyncs = 0
        mistakes = 0
        rewritten_lines: set[int] = set()
        rewritten_blocks: set[tuple[int, int]] = set()
        i, own_indent = self._sync(body)
        while i < len(body):
            line = body[i]
            self.on_progress(path, i + 1, len(body))
            start = 0 if own_indent else len(line) - len(line.lstrip(" "))

            # Formatting mistake: over-indent the line, pause, then fix it.
            if self.s.format_rewrite_rate > 0:
                indent = len(line) - len(line.lstrip(" "))
                if indent > 0 and self.rng.random() < self.s.format_rewrite_rate:
                    start = self._format_slip(indent, own_indent)
                    mistakes += 1

            restarted = False
            for col in range(start, len(line)):
                self._ready(check_title=True)
                if self._dirty:
                    restarted = True
                    break
                if line_offsets[i] + col in skip:
                    continue
                self._human_char(line[col])

            if restarted or self._dirty:
                resyncs += 1
                i, own_indent = self._sync(body)
                continue

            self._newline()
            if self.s.line_pause:
                self._pause(self.s.line_pause, 0.8)

            # Mistake: delete the whole line just typed and rewrite it.
            if (
                i not in rewritten_lines
                and self.s.line_rewrite_rate > 0
                and self.rng.random() < self.s.line_rewrite_rate
            ):
                rewritten_lines.add(i)
                self._delete_last_line()
                mistakes += 1
                i, own_indent = i, True
                continue

            # Mistake: delete a whole method/class/XML element and rewrite it.
            starts = block_ends.get(i)
            if starts and self.s.block_rewrite_rate > 0:
                start_i = max(starts)
                if (start_i, i) not in rewritten_blocks and self.rng.random() < self.s.block_rewrite_rate:
                    rewritten_blocks.add((start_i, i))
                    self._delete_block(start_i)
                    mistakes += 1
                    i, own_indent = start_i, True
                    continue

            own_indent = False
            i += 1

        if self.s.reformat:
            self._cmd("reformat")
            self.sleep(0.5)
        self._save()
        disk = self._read_disk()
        if normalize(disk) == normalize(target):
            if self.s.exact_whitespace and _lines(disk) != _lines(target):
                # Same code, IDE indented it differently: snap to the exact target.
                return self._paste_file(path, target, len(body), resyncs, status="typed", mistakes=mistakes)
            return FileOutcome(path, "typed", len(body), resyncs, mistakes)
        if not self.s.repair:
            return FileOutcome(path, "mismatch", len(body), resyncs, mistakes)
        return self._paste_file(path, target, len(body), resyncs, status="repaired", mistakes=mistakes)

    def _paste_file(self, path: str, target: str, lines: int, resyncs: int, status: str, mistakes: int = 0) -> FileOutcome:
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
        return FileOutcome(path, status if ok else "mismatch", lines, resyncs, mistakes)
