"""Settings: defaults < ~/.config/autotyper/config.toml < CLI flags."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("AUTOTYPER_CONFIG", "~/.config/autotyper/config.toml")).expanduser()


@dataclass
class Keymap:
    """IDE shortcuts as chords. Defaults = JetBrains "macOS" keymap (Android Studio default)."""

    select_all: str = "cmd+a"
    select_to_doc_end: str = "cmd+shift+end"
    doc_end: str = "cmd+end"
    delete_forward: str = "forward_delete"
    goto_line: str = "cmd+l"
    save: str = "cmd+s"
    paste: str = "cmd+v"
    reformat: str = "cmd+alt+l"
    escape: str = "escape"
    newline: str = "return"


@dataclass
class Settings:
    # --- models -------------------------------------------------------
    orchestrator_model: str = "claude-opus-5-5"
    orchestrator_effort: str = "xhigh"
    worker_model: str = "claude-haiku-5-5"
    worker_effort: str = "medium"
    review: bool = True  # orchestrator reviews worker output for cross-file consistency
    max_parallel_workers: int = 4

    # --- typing -------------------------------------------------------
    wpm: int = 110  # 1 word = 5 chars
    jitter: float = 0.35  # +/- fraction of per-char delay
    line_pause: float = 0.12  # extra pause after each line, seconds
    key_gap: float = 0.03  # pause between keys of an editor command
    instant: bool = False  # paste whole files instead of typing
    repair: bool = True  # paste the exact target if the typed result differs
    reformat: bool = False  # run IDE "Reformat Code" after each file
    exact_whitespace: bool = True  # if only indentation differs at the end, paste the exact target
    xml_attr_quotes: bool = True  # Android Studio inserts "" after = in XML attributes

    # --- focus gate ---------------------------------------------------
    ide_bundle_ids: list[str] = field(
        default_factory=lambda: [
            "com.google.android.studio",
            "com.google.android.studio-EAP",
            "com.jetbrains.intellij",
            "com.jetbrains.intellij.ce",
        ]
    )
    ide_app_path: str | None = None  # auto-detected when empty
    resume_idle: float = 1.2  # seconds without your keyboard/mouse input before typing resumes
    auto_refocus: bool = True  # reopen the target file if you sit idle on another tab
    refocus_after: float = 4.0
    poll_interval: float = 0.15

    keymap: Keymap = field(default_factory=Keymap)

    def char_delay(self) -> float:
        return 60.0 / (max(self.wpm, 1) * 5)


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    s = Settings()
    if path.exists():
        data = tomllib.loads(path.read_text())
        apply_overrides(s, data)
    return s


def apply_overrides(s: Settings, data: dict) -> Settings:
    """Apply a (possibly sectioned) mapping. Sections are flattened; [keymap] maps to Keymap."""
    known = {f.name for f in fields(Settings)}
    for key, value in data.items():
        if key == "keymap" and isinstance(value, dict):
            for k, v in value.items():
                if hasattr(s.keymap, k):
                    setattr(s.keymap, k, str(v))
        elif isinstance(value, dict):
            apply_overrides(s, value)
        elif key in known and value is not None:
            setattr(s, key, value)
    return s


SAMPLE_CONFIG = """\
# ~/.config/autotyper/config.toml
[models]
orchestrator_model = "claude-opus-5-5"
orchestrator_effort = "xhigh"
worker_model = "claude-haiku-5-5"
worker_effort = "medium"
review = true

[typing]
wpm = 110
jitter = 0.35
repair = true
reformat = false

[focus]
resume_idle = 1.2
auto_refocus = true

[keymap]   # JetBrains macOS keymap defaults
goto_line = "cmd+l"
select_to_doc_end = "cmd+shift+end"
"""
