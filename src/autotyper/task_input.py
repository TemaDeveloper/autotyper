"""Load a coding task from several sources and convert it to Messages API content blocks."""

from __future__ import annotations

import base64
import html
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 30 * 1024 * 1024
BINARY_SNIFF_BYTES = 8 * 1024

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
PDF_MEDIA_TYPE = "application/pdf"
HTML_SUFFIXES = {".html", ".htm"}

NO_TASK_HINT = (
    "No task given. Provide one of: --task \"...\", --file <path> (repeatable), "
    "--clipboard, or pipe the task on stdin."
)


class TaskInputError(Exception):
    """Raised when a task cannot be loaded or is empty."""


@dataclass
class Attachment:
    kind: Literal["image", "pdf"]
    media_type: str  # image/png, image/jpeg, image/gif, image/webp, application/pdf
    data_b64: str  # base64, no newlines
    name: str


@dataclass
class TaskInput:
    text: str  # combined task text (may be "" if only attachments)
    attachments: list[Attachment] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # e.g. ["--task text", "file: hw3.pdf"]

    def is_empty(self) -> bool:
        return not self.text.strip() and not self.attachments


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1\s*>", "", raw)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _check_image_size(raw: bytes, name: str) -> None:
    if len(raw) > MAX_IMAGE_BYTES:
        raise TaskInputError(
            f"image too large: {name} is {len(raw)} bytes (limit {MAX_IMAGE_BYTES} bytes)"
        )


def _load_file(path_arg: str | Path, parts: list[str], attachments: list[Attachment], sources: list[str]) -> None:
    path = Path(path_arg).expanduser()
    if not path.is_file():
        raise TaskInputError(f"file not found: {path}")
    name = path.name
    suffix = path.suffix.lower()
    raw = path.read_bytes()

    if suffix in IMAGE_MEDIA_TYPES:
        _check_image_size(raw, name)
        attachments.append(
            Attachment(kind="image", media_type=IMAGE_MEDIA_TYPES[suffix], data_b64=_b64(raw), name=name)
        )
        sources.append(f"file: {name}")
        return

    if suffix == ".pdf":
        if len(raw) > MAX_PDF_BYTES:
            raise TaskInputError(
                f"PDF too large: {name} is {len(raw)} bytes (limit {MAX_PDF_BYTES} bytes)"
            )
        attachments.append(
            Attachment(kind="pdf", media_type=PDF_MEDIA_TYPE, data_b64=_b64(raw), name=name)
        )
        sources.append(f"file: {name}")
        return

    if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
        raise TaskInputError(f"unsupported binary file: {name}")

    text = raw.decode("utf-8", errors="replace")
    if suffix in HTML_SUFFIXES:
        text = _html_to_text(text)
    else:
        text = text.strip()

    if text:
        parts.append(f"### From file: {name}\n\n{text}")
    sources.append(f"file: {name}")


def read_macos_clipboard() -> tuple[str | None, bytes | None]:
    """Return (text, png_bytes) from the macOS clipboard; either may be None."""
    try:
        from AppKit import (
            NSBitmapImageFileTypePNG,
            NSBitmapImageRep,
            NSPasteboard,
            NSPasteboardTypePNG,
            NSPasteboardTypeString,
            NSPasteboardTypeTIFF,
        )
    except ImportError:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return None, None
        return (result.stdout or None), None

    pasteboard = NSPasteboard.generalPasteboard()
    text = pasteboard.stringForType_(NSPasteboardTypeString)
    text_out = str(text) if text else None

    png_data = pasteboard.dataForType_(NSPasteboardTypePNG)
    if png_data is None:
        tiff_data = pasteboard.dataForType_(NSPasteboardTypeTIFF)
        if tiff_data is not None:
            rep = NSBitmapImageRep.imageRepWithData_(tiff_data)
            if rep is not None:
                png_data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    png_out = bytes(png_data) if png_data is not None else None
    return text_out, png_out


def load_task(
    text: str | None = None,
    files: Sequence[str | Path] = (),
    clipboard: bool = False,
    stdin_text: str | None = None,
    clipboard_reader: Callable[[], tuple[str | None, bytes | None]] | None = None,
) -> TaskInput:
    parts: list[str] = []
    attachments: list[Attachment] = []
    sources: list[str] = []

    if text is not None and text.strip():
        parts.append(text.strip())
        sources.append("--task text")

    if stdin_text is not None and stdin_text.strip():
        parts.append(stdin_text.strip())
        sources.append("stdin")

    for path_arg in files:
        _load_file(path_arg, parts, attachments, sources)

    if clipboard:
        reader = clipboard_reader or read_macos_clipboard
        clip_text, clip_png = reader()
        if clip_text and clip_text.strip():
            parts.append(f"### From clipboard\n\n{clip_text.strip()}")
            sources.append("clipboard (text)")
        if clip_png:
            _check_image_size(clip_png, "clipboard.png")
            attachments.append(
                Attachment(kind="image", media_type="image/png", data_b64=_b64(clip_png), name="clipboard.png")
            )
            sources.append("clipboard (image)")
        if not (clip_text and clip_text.strip()) and not clip_png:
            raise TaskInputError("clipboard is empty")

    task = TaskInput(text="\n\n".join(parts), attachments=attachments, sources=sources)
    if task.is_empty():
        raise TaskInputError(NO_TASK_HINT)
    return task


def to_content_blocks(task: TaskInput) -> list[dict]:
    blocks: list[dict] = []
    for att in task.attachments:
        if att.kind == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": att.media_type, "data": att.data_b64},
                }
            )
        else:
            blocks.append(
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": PDF_MEDIA_TYPE, "data": att.data_b64},
                }
            )
    body = task.text if task.text.strip() else "The task is in the attached file(s)."
    blocks.append({"type": "text", "text": body})
    return blocks
