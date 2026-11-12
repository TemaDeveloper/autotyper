"""Tests for autotyper.task_input. Never touches the real clipboard."""

from __future__ import annotations

import base64

import pytest

from autotyper import task_input
from autotyper.task_input import TaskInputError, load_task, to_content_blocks

# 1x1 transparent PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def no_clipboard():
    return None, None


def test_text_arg_and_stdin_are_combined_and_stripped():
    task = load_task(text="  Solve problem 3  ", stdin_text="\n extra notes \n")
    assert task.text == "Solve problem 3\n\nextra notes"
    assert task.sources == ["--task text", "stdin"]
    assert task.attachments == []
    assert not task.is_empty()


def test_empty_text_parts_are_skipped():
    task = load_task(text="   ", stdin_text="hello")
    assert task.text == "hello"
    assert task.sources == ["stdin"]


def test_multiple_files_txt_html_png_pdf(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("Implement a login screen.\n", encoding="utf-8")

    html_file = tmp_path / "spec.html"
    html_file.write_text(
        "<html><head><style>body{color:red}</style><script>alert('x')</script></head>"
        "<body><h1>Spec &amp; goals</h1><p>Use &lt;Button&gt;.</p></body></html>",
        encoding="utf-8",
    )

    png = tmp_path / "screen.png"
    png.write_bytes(TINY_PNG)

    pdf = tmp_path / "hw3.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf body")

    task = load_task(
        text="Main task",
        files=[txt, html_file, png, pdf],
        clipboard_reader=no_clipboard,
    )

    assert "### From file: notes.txt" in task.text
    assert "Implement a login screen." in task.text
    assert "### From file: spec.html" in task.text
    assert "Spec & goals" in task.text
    assert "Use <Button>." in task.text
    assert "alert" not in task.text
    assert "color:red" not in task.text
    assert "<h1>" not in task.text

    assert [a.kind for a in task.attachments] == ["image", "pdf"]
    assert task.attachments[0].media_type == "image/png"
    assert task.attachments[0].name == "screen.png"
    assert base64.b64decode(task.attachments[0].data_b64) == TINY_PNG
    assert task.attachments[1].media_type == "application/pdf"
    assert task.attachments[1].name == "hw3.pdf"

    assert task.sources == [
        "--task text",
        "file: notes.txt",
        "file: spec.html",
        "file: screen.png",
        "file: hw3.pdf",
    ]


def test_file_path_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "task.txt").write_text("from home", encoding="utf-8")
    task = load_task(files=["~/task.txt"], clipboard_reader=no_clipboard)
    assert "from home" in task.text


def test_missing_file_raises_with_path(tmp_path):
    missing = tmp_path / "does-not-exist.txt"
    with pytest.raises(TaskInputError) as exc:
        load_task(files=[missing], clipboard_reader=no_clipboard)
    assert str(missing) in str(exc.value)


def test_binary_file_rejected(tmp_path):
    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\x00\x01\x02binary\x00payload")
    with pytest.raises(TaskInputError, match="unsupported binary file"):
        load_task(files=[binary], clipboard_reader=no_clipboard)


def test_oversize_image_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(task_input, "MAX_IMAGE_BYTES", 10)
    big = tmp_path / "big.png"
    big.write_bytes(TINY_PNG)  # larger than the patched 10-byte limit
    with pytest.raises(TaskInputError, match="image too large"):
        load_task(files=[big], clipboard_reader=no_clipboard)


def test_oversize_pdf_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(task_input, "MAX_PDF_BYTES", 10)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4 " + b"x" * 50)
    with pytest.raises(TaskInputError, match="PDF too large"):
        load_task(files=[big], clipboard_reader=no_clipboard)


def test_clipboard_text(tmp_path):
    task = load_task(clipboard=True, clipboard_reader=lambda: ("copied task", None))
    assert task.text == "### From clipboard\n\ncopied task"
    assert task.sources == ["clipboard (text)"]
    assert task.attachments == []


def test_clipboard_image(tmp_path):
    task = load_task(clipboard=True, clipboard_reader=lambda: (None, TINY_PNG))
    assert task.text == ""
    assert len(task.attachments) == 1
    att = task.attachments[0]
    assert att.kind == "image"
    assert att.media_type == "image/png"
    assert att.name == "clipboard.png"
    assert base64.b64decode(att.data_b64) == TINY_PNG
    assert task.sources == ["clipboard (image)"]


def test_clipboard_text_and_image_together():
    task = load_task(clipboard=True, clipboard_reader=lambda: ("see screenshot", TINY_PNG))
    assert "see screenshot" in task.text
    assert len(task.attachments) == 1
    assert task.sources == ["clipboard (text)", "clipboard (image)"]


def test_clipboard_empty_raises():
    with pytest.raises(TaskInputError, match="clipboard is empty"):
        load_task(clipboard=True, clipboard_reader=no_clipboard)


def test_clipboard_whitespace_only_raises():
    with pytest.raises(TaskInputError, match="clipboard is empty"):
        load_task(clipboard=True, clipboard_reader=lambda: ("   \n", None))


def test_clipboard_reader_not_called_when_disabled():
    def boom():
        raise AssertionError("clipboard should not be read")

    task = load_task(text="typed", clipboard=False, clipboard_reader=boom)
    assert task.text == "typed"


def test_no_input_raises_helpful_error():
    with pytest.raises(TaskInputError, match="No task given"):
        load_task(clipboard_reader=no_clipboard)


def test_whitespace_only_input_raises():
    with pytest.raises(TaskInputError, match="No task given"):
        load_task(text="  ", stdin_text="\n\n", clipboard_reader=no_clipboard)


def test_empty_file_only_raises(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(TaskInputError, match="No task given"):
        load_task(files=[empty], clipboard_reader=no_clipboard)


def test_is_empty_with_only_attachment():
    task = load_task(clipboard=True, clipboard_reader=lambda: (None, TINY_PNG))
    assert task.text == ""
    assert not task.is_empty()


def test_to_content_blocks_ordering_and_shapes(tmp_path):
    png = tmp_path / "a.png"
    png.write_bytes(TINY_PNG)
    pdf = tmp_path / "b.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")
    task = load_task(text="Do the thing", files=[png, pdf], clipboard_reader=no_clipboard)

    blocks = to_content_blocks(task)

    assert [b["type"] for b in blocks] == ["image", "document", "text"]
    assert blocks[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(TINY_PNG).decode("ascii"),
        },
    }
    assert blocks[1] == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(b"%PDF-1.4 body").decode("ascii"),
        },
    }
    assert blocks[2] == {"type": "text", "text": task.text}
    assert "Do the thing" in blocks[2]["text"]


def test_to_content_blocks_attachment_only_uses_placeholder_text():
    task = load_task(clipboard=True, clipboard_reader=lambda: (None, TINY_PNG))
    blocks = to_content_blocks(task)
    assert blocks[-1] == {"type": "text", "text": "The task is in the attached file(s)."}
    assert blocks[0]["type"] == "image"


def test_base64_has_no_newlines(tmp_path):
    big_ish = tmp_path / "pic.png"
    big_ish.write_bytes(TINY_PNG * 500)  # long enough that MIME-style wrapping would show up
    task = load_task(files=[big_ish], clipboard_reader=no_clipboard)
    for att in task.attachments:
        assert "\n" not in att.data_b64
        assert "\r" not in att.data_b64
    for block in to_content_blocks(task):
        if block["type"] in ("image", "document"):
            assert "\n" not in block["source"]["data"]


def test_text_only_content_blocks():
    task = load_task(text="just text", clipboard_reader=no_clipboard)
    assert to_content_blocks(task) == [{"type": "text", "text": "just text"}]
