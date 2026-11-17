import random
from pathlib import Path

import pytest

from autotyper.config import Settings
from autotyper.typer.engine import (
    TypingEngine,
    block_starts,
    normalize,
    plausible_typo,
    xml_skip_quote_positions,
)
from autotyper.typer.fake_editor import FakeJetBrainsEditor
from autotyper.typer.gate import FocusGate, FocusState

KOTLIN = '''package com.example.demo

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity

// Counts button taps and shows the total.
class MainActivity : AppCompatActivity() {
    private var count = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        val label = findViewById<TextView>(R.id.label)
        findViewById<Button>(R.id.button).setOnClickListener {
            count++
            label.text = "Taps: ${count}"
        }
        val items = listOf("a", "b", "c").map { it.uppercase() }
        if (items.isNotEmpty() && (count > 0 || items[0] == "A")) {
            println(items)
        }
    }
}
'''

XML = '''<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical">

    <!-- The tap counter -->
    <TextView
        android:id="@+id/label"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content"
        android:text="@string/hello" />

    <Button
        android:id="@+id/button"
        android:layout_width="wrap_content"
        android:layout_height="wrap_content">
    </Button>
</LinearLayout>
'''


class AlwaysFront:
    def __init__(self):
        self.front = True
        self.calls = 0
        self.on_call = None

    def state(self, ids):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        bundle = "com.google.android.studio" if self.front else "com.apple.Safari"
        return FocusState(bundle, "Android Studio" if self.front else "Safari", True, None)


class Monitor:
    available = True
    paused_by_hotkey = False
    abort_requested = False

    def __init__(self, clock):
        self.clock = clock
        self.last = 0.0

    def last_user_event_time(self):
        return self.last


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.001)


def make(tmp_path, settings=None, probe=None, smart=None):
    clock = Clock()
    ed = FakeJetBrainsEditor(smart=smart) if smart else FakeJetBrainsEditor()
    probe = probe or AlwaysFront()
    mon = Monitor(clock)
    s = settings or Settings()
    gate = FocusGate(probe, mon, s.ide_bundle_ids, resume_idle=0.5, poll_interval=0.1, clock=clock, sleep=clock.sleep)
    eng = TypingEngine(ed, ed, ed, gate, s, sleep=clock.sleep, rng=random.Random(0))
    return eng, ed, probe, mon, clock


@pytest.mark.parametrize("name,content", [("MainActivity.kt", KOTLIN), ("activity_main.xml", XML)])
def test_types_new_file_exactly(tmp_path, name, content):
    eng, ed, *_ = make(tmp_path)
    path = str(tmp_path / "app" / name)
    out = eng.type_file(path, content)
    assert out.status == "typed", ed.text
    assert normalize(Path(path).read_text()) == normalize(content)
    assert "/*completion*/" not in ed.text


def test_modifies_existing_file_keeping_prefix(tmp_path):
    eng, ed, *_ = make(tmp_path)
    path = tmp_path / "MainActivity.kt"
    old = KOTLIN.replace("    private var count = 0\n", "    private var count = 5\n    // old\n")
    path.write_text(old)
    out = eng.type_file(str(path), KOTLIN)
    assert out.status == "typed"
    assert normalize(path.read_text()) == normalize(KOTLIN)


def test_unchanged_file_is_skipped(tmp_path):
    eng, ed, *_ = make(tmp_path)
    path = tmp_path / "A.kt"
    path.write_text(KOTLIN)
    assert eng.type_file(str(path), KOTLIN).status == "unchanged"


def test_pauses_when_ide_not_front_and_resyncs_after_caret_moved(tmp_path):
    probe = AlwaysFront()
    eng, ed, probe, mon, clock = make(tmp_path, probe=probe)
    events = {}

    def on_call(n):
        # Mid-file: user swipes to the browser, comes back and clicks at doc start.
        if n == 300:
            probe.front = False
            events["left"] = True
        if n == 320:
            probe.front = True
            ed.caret = 0  # their click moved the caret
            ed.anchor = None
            mon.last = clock()

    probe.on_call = on_call
    path = tmp_path / "MainActivity.kt"
    out = eng.type_file(str(path), KOTLIN)
    assert events.get("left")
    assert out.resyncs >= 1
    assert out.status == "typed", ed.text
    assert normalize(path.read_text()) == normalize(KOTLIN)


def test_repair_when_typing_gets_mangled(tmp_path):
    # Engine types quotes normally, but this simulated IDE still auto-inserts
    # "" after '=' in XML attributes, so the quotes double up; repair must fix it.
    from autotyper.typer.fake_editor import SmartKeys

    s = Settings(xml_attr_quotes=False)
    eng, ed, *_ = make(tmp_path, settings=s, smart=SmartKeys(xml_attr_quotes=True))
    path = tmp_path / "activity_main.xml"
    out = eng.type_file(str(path), XML)
    assert out.status == "repaired"
    assert normalize(path.read_text()) == normalize(XML)
    assert ed.clipboard != XML  # user's clipboard restored after the repair paste


def test_instant_mode_pastes(tmp_path):
    eng, ed, *_ = make(tmp_path, settings=Settings(instant=True))
    path = tmp_path / "A.kt"
    assert eng.type_file(str(path), KOTLIN).status == "typed"
    assert path.read_text().strip() == KOTLIN.strip()


def test_xml_skip_positions():
    t = '<a b="1" c=\'2\'>x="y"</a><!-- d="e" -->'
    pos = xml_skip_quote_positions(t)
    assert [t[i - 1 : i + 2] for i in sorted(pos)] == ['="1']


def test_plausible_typo_is_a_neighbour_and_keeps_case():
    import random

    assert plausible_typo(random.Random(0), "a") in ("s", "q")
    wrong = plausible_typo(random.Random(0), "K")
    assert wrong in ("J", "L")
    assert plausible_typo(random.Random(0), "{") is None
    assert plausible_typo(random.Random(0), "5") in ("4", "6")


def test_typos_are_backspaced_and_corrected(tmp_path):
    s = Settings(typo_rate=1.0)
    eng, ed, *_ = make(tmp_path, settings=s)
    path = tmp_path / "MainActivity.kt"
    out = eng.type_file(str(path), KOTLIN)
    assert out.status == "typed", ed.text
    assert normalize(path.read_text()) == normalize(KOTLIN)
    assert ("press", "delete", ()) in ed.events


def test_line_rewrite_deletes_and_retypes_the_line(tmp_path):
    s = Settings(typo_rate=0, line_rewrite_rate=1.0, block_rewrite_rate=0, format_rewrite_rate=0)
    eng, ed, *_ = make(tmp_path, settings=s)
    path = tmp_path / "A.kt"
    out = eng.type_file(str(path), KOTLIN)
    assert out.status == "typed", ed.text
    assert normalize(path.read_text()) == normalize(KOTLIN)
    # up, home, shift+end, forward_delete happen at least once
    assert ("press", "up", ()) in ed.events
    assert ("press", "home", ()) in ed.events
    assert ("press", "end", ("shift",)) in ed.events
    assert out.mistakes >= 1


def test_full_line_rewrite_is_off_by_default(tmp_path):
    assert Settings().line_rewrite_rate == 0
    eng, ed, *_ = make(tmp_path)
    path = tmp_path / "MainActivity.kt"
    out = eng.type_file(str(path), KOTLIN)
    assert out.status == "typed", ed.text
    assert ("press", "up", ()) not in ed.events  # no whole-line deletes


def test_human_delays_change_gear_from_key_to_key(tmp_path):
    eng, *_ = make(tmp_path)
    delays = [eng._human_delay() for _ in range(40)]
    # Not a metronome: several distinct delays and a wide spread between gears.
    assert len(set(round(d, 3) for d in delays)) >= 10
    assert max(delays) > min(delays) * 2


def test_block_rewrite_rewrites_whole_method(tmp_path):
    s = Settings(typo_rate=0, line_rewrite_rate=0, block_rewrite_rate=1.0, format_rewrite_rate=0)
    eng, ed, *_ = make(tmp_path, settings=s)
    path = tmp_path / "MainActivity.kt"
    out = eng.type_file(str(path), KOTLIN)
    assert out.status == "typed", ed.text
    assert normalize(path.read_text()) == normalize(KOTLIN)
    assert out.mistakes >= 1


def test_xml_block_rewrite_rewrites_whole_element(tmp_path):
    s = Settings(typo_rate=0, line_rewrite_rate=0, block_rewrite_rate=1.0, format_rewrite_rate=0)
    eng, ed, *_ = make(tmp_path, settings=s)
    path = tmp_path / "activity_main.xml"
    out = eng.type_file(str(path), XML)
    assert out.status == "typed", ed.text
    assert normalize(path.read_text()) == normalize(XML)
    assert out.mistakes >= 1


def test_format_slip_overindents_then_fixes(tmp_path):
    s = Settings(typo_rate=0, line_rewrite_rate=0, block_rewrite_rate=0, format_rewrite_rate=1.0)
    eng, ed, *_ = make(tmp_path, settings=s)
    path = tmp_path / "MainActivity.kt"
    out = eng.type_file(str(path), KOTLIN)
    assert out.status == "typed", ed.text
    assert normalize(path.read_text()) == normalize(KOTLIN)
    assert out.mistakes >= 1


def test_block_starts_finds_methods_classes_and_xml_elements():
    kotlin = KOTLIN.split("\n")
    ends = block_starts(kotlin, False)
    # KOTLIN's class ends on the line whose stripped text is the final "}".
    last_brace = max(i for i, line in enumerate(kotlin) if line.strip() == "}")
    assert last_brace in ends

    xml = XML.split("\n")
    xends = block_starts(xml, True)
    assert xends  # at least one multi-line XML element
