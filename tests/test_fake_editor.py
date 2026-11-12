from __future__ import annotations

from pathlib import Path

from autotyper.typer.fake_editor import FakeJetBrainsEditor, SmartKeys


def type_text(ed: FakeJetBrainsEditor, s: str) -> None:
    for ch in s:
        ed.type_char(ch)


def open_in(ed: FakeJetBrainsEditor, tmp_path: Path, name: str, content: str = "") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    ed.open_file(str(p))
    return p


def test_bracket_pairing_and_overtype() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "foo(a)")
    assert ed.text == "foo(a)"
    assert ed.caret == 6
    assert ed.auto_closers == set()


def test_bracket_pairing_can_be_disabled() -> None:
    ed = FakeJetBrainsEditor(SmartKeys(pair_brackets=False))
    type_text(ed, "f(")
    assert ed.text == "f("


def test_quote_pairing_and_overtype() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, 'x = "hi"')
    assert ed.text == 'x = "hi"'
    assert ed.caret == len(ed.text)


def test_quote_after_word_char_is_not_paired() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "don't")
    assert ed.text == "don't"


def test_auto_closer_index_follows_inserts_before_it() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "(")
    assert ed.text == "()"
    ed.press("left")
    type_text(ed, "x")  # inserted before the auto ')' -> its index must shift
    assert ed.text == "x()"
    ed.press("right")  # caret now between '(' and ')'
    ed.type_char(")")
    assert ed.text == "x()"
    assert ed.caret == 3


def test_enter_between_braces_puts_closer_on_own_line() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "{")
    assert ed.text == "{}"
    ed.press("return")
    assert ed.text == "{\n    \n}"
    assert ed.caret == 6
    assert ed.text[: ed.caret].count("\n") == 1  # caret on the indented middle line


def test_enter_after_unmatched_brace_closes_block() -> None:
    ed = FakeJetBrainsEditor(SmartKeys(pair_brackets=False))
    type_text(ed, "{")
    ed.press("return")
    assert ed.text == "{\n    \n}"
    assert ed.caret == 6


def test_enter_after_unmatched_brace_respects_flag() -> None:
    ed = FakeJetBrainsEditor(SmartKeys(pair_brackets=False, enter_closes_brace=False))
    type_text(ed, "{")
    ed.press("return")
    assert ed.text == "{\n    "


def test_closing_brace_dedents_whitespace_line() -> None:
    ed = FakeJetBrainsEditor(SmartKeys(pair_brackets=False, enter_closes_brace=False))
    type_text(ed, "{")
    ed.press("return")
    type_text(ed, "}")
    assert ed.text == "{\n}"
    assert ed.caret == 3


def test_enter_with_popup_inserts_completion_but_escape_avoids_it() -> None:
    corrupted = FakeJetBrainsEditor()
    type_text(corrupted, "foo")
    assert corrupted.popup_open is True
    corrupted.press("return")
    assert corrupted.text == "foo/*completion*/"
    assert corrupted.popup_open is False

    safe = FakeJetBrainsEditor()
    type_text(safe, "foo")
    safe.press("escape")
    assert safe.popup_open is False
    safe.press("return")
    assert safe.text == "foo\n"


def test_non_identifier_char_closes_popup() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "foo(")
    assert ed.popup_open is False


def test_goto_line_and_column(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "a.txt", "alpha\nbeta\ngamma")
    ed.press("l", ("cmd",))
    assert ed.goto_buffer == ""
    type_text(ed, "3:1")
    assert ed.text == "alpha\nbeta\ngamma"  # typing went to the dialog, not the document
    ed.press("return")
    assert ed.goto_buffer is None
    assert ed.caret == len("alpha\nbeta\n")
    assert ed.text[ed.caret] == "g"

    ed.press("l", ("cmd",))
    type_text(ed, "2:99")
    ed.press("return")
    assert ed.caret == len("alpha\nbeta")  # column clamps to line length


def test_escape_cancels_goto(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "a.txt", "abc")
    ed.press("l", ("cmd",))
    type_text(ed, "9")
    ed.press("escape")
    assert ed.goto_buffer is None
    assert ed.caret == 0


def test_cmd_shift_end_then_forward_delete(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "a.txt", "hello world")
    for _ in range(6):
        ed.press("right")
    assert ed.caret == 6
    ed.press("end", ("cmd", "shift"))
    assert ed.anchor == 6
    assert ed.caret == len("hello world")
    ed.press("forward_delete")
    assert ed.text == "hello "
    assert ed.caret == 6
    assert ed.anchor is None


def test_cmd_a_then_paste_replaces_selection(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "a.txt", "")
    type_text(ed, "abc")
    ed.set_text("XYZ")
    ed.press("a", ("cmd",))
    assert (ed.anchor, ed.caret) == (0, 3)
    ed.press("v", ("cmd",))
    assert ed.text == "XYZ"
    assert ed.caret == 3


def test_paste_is_verbatim_without_smart_keys(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "a.txt", "")
    ed.set_text("({\"")
    ed.press("v", ("cmd",))
    assert ed.text == '({"'
    assert ed.caret == 3


def test_cmd_s_writes_to_disk(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    p = open_in(ed, tmp_path, "Main.java", "")
    type_text(ed, "abc")
    ed.press("s", ("cmd",))
    assert p.read_text(encoding="utf-8") == "abc"


def test_open_file_autosaves_previous_and_creates_missing(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    first = open_in(ed, tmp_path, "first.txt", "")
    type_text(ed, "hello")
    second = tmp_path / "sub" / "second.txt"
    second.parent.mkdir()
    second.write_text("other", encoding="utf-8")
    ed.open_file(str(second))
    assert first.read_text(encoding="utf-8") == "hello"
    assert ed.text == "other"
    assert ed.caret == 0
    assert ed.path == str(second)

    missing = tmp_path / "new" / "made.txt"
    ed.open_file(str(missing))
    assert missing.exists()
    assert ed.text == ""
    assert second.read_text(encoding="utf-8") == "other"


def test_xml_close_tag_inserted_on_gt(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "layout.xml", "")
    type_text(ed, "<TextView>")
    assert ed.text == "<TextView></TextView>"
    assert ed.caret == len("<TextView>")


def test_xml_attribute_equals_inserts_quotes(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "layout.xml", "")
    type_text(ed, '<TextView android:text=')
    assert ed.text == '<TextView android:text=""'
    assert ed.caret == len(ed.text) - 1
    ed.type_char('"')  # overtypes the auto-inserted closing quote
    assert ed.text == '<TextView android:text=""'
    assert ed.caret == len(ed.text)


def test_xml_self_close_inserts_gt_and_gt_overtypes(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "layout.xml", "")
    type_text(ed, "<Button /")
    assert ed.text == "<Button />"
    assert ed.caret == len("<Button /")  # caret sits just before the inserted '>'
    type_text(ed, ">")
    assert ed.text == "<Button />"
    assert ed.caret == len("<Button />")


def test_xml_rules_only_apply_to_xml_files(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor(SmartKeys(pair_quotes=False))
    open_in(ed, tmp_path, "Main.java", "")
    type_text(ed, "<TextView>")
    assert ed.text == "<TextView>"


def test_doc_comment_stub_on_enter_after_slash_star_star() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "/**")
    ed.press("return")
    assert ed.text == "/**\n * \n */"
    assert ed.caret == len("/**\n * ")
    assert ed.text[: ed.caret].count("\n") == 1


def test_doc_comment_stub_keeps_indent() -> None:
    ed = FakeJetBrainsEditor()
    type_text(ed, "    /**")
    ed.press("return")
    assert ed.text == "    /**\n     * \n     */"


def test_reformat_reindents_by_bracket_depth(tmp_path: Path) -> None:
    ed = FakeJetBrainsEditor()
    open_in(ed, tmp_path, "A.java", "class A {\nint x;   \n}")
    ed.press("l", ("cmd", "alt"))
    assert ed.text == "class A {\n    int x;\n}"


def test_events_are_recorded() -> None:
    ed = FakeJetBrainsEditor()
    ed.type_char("a")
    ed.press("return", ())
    assert ed.events[0] == ("type_char", "a")
    assert ed.events[1] == ("press", "return", ())
