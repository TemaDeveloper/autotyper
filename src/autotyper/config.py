"""Settings: defaults < ~/.config/autotyper/config.toml < CLI flags."""

from __future__ import annotations

import json
import os
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("AUTOTYPER_CONFIG", "~/.config/autotyper/config.toml")).expanduser()


@dataclass
class Keymap:
    """IDE shortcuts as chords. Defaults = JetBrains "macOS" keymap (Android Studio default)."""

    select_all: str = "cmd+a"
    select_to_doc_end: str = "cmd+shift+end"
    select_to_line_end: str = "shift+end"
    delete_to_line_start: str = "cmd+delete"
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
    # --- AI provider -------------------------------------------------
    provider: str = "claude"  # claude | ollama | gemini | openrouter
    ollama_model: str = "qwen2.5-coder:14b"
    ollama_url: str = "http://localhost:11434"
    gemini_model: str = "gemini-flash-latest"
    openrouter_model: str = ""  # picked from the current free models by `autotyper login`
    deepseek_model: str = "deepseek-chat"  # deepseek-chat (fast/coding) | deepseek-reasoner (slower, deeper)

    # --- models (Claude) ----------------------------------------------
    orchestrator_model: str = "claude-opus-5-5"
    orchestrator_effort: str = "xhigh"
    worker_model: str = "claude-haiku-5-5"
    worker_effort: str = "medium"
    review: bool = True  # orchestrator reviews worker output for cross-file consistency
    max_parallel_workers: int = 4

    # --- typing -------------------------------------------------------
    wpm: int = 40  # 1 word = 5 chars
    jitter: float = 0.45  # log-normal spread of per-key delays (larger = more uneven)
    line_pause: float = 0.5  # extra pause after each line, seconds
    word_pause: float = 0.12  # extra pause after each space, seconds
    hesitation: float = 0.03  # chance per character of a longer "thinking" pause
    hesitation_pause: float = 0.9  # base length of that pause, seconds
    typo_rate: float = 0.02  # chance per character of a single miskey, corrected right away
    line_rewrite_rate: float = 0.0  # chance per line of deleting it and retyping it (off by default)
    block_rewrite_rate: float = 0.05  # chance per method/class/XML element of a quick in-place formatting fix (never deletes the block)
    format_rewrite_rate: float = 0.03  # chance per indented line of a mis-indent, corrected right away
    key_gap: float = 0.03  # pause between keys of an editor command
    instant: bool = False  # paste whole files instead of typing
    repair: bool = True  # paste the exact target if the typed result differs
    reformat: bool = False  # run IDE "Reformat Code" after each file
    exact_whitespace: bool = True  # if only indentation differs at the end, paste the exact target
    xml_attr_quotes: bool = False  # if your IDE auto-inserts "" after = in XML, set true to skip typing the opening quote
    kotlin_only: bool = True  # type only Kotlin + layout XML; skip values/resources (strings.xml, colors.xml, ...)
    time_limit_minutes: float = 30.0  # pace all files so typing finishes within this many minutes
    hud: bool = False  # floating status overlay (opt-in; off by default so nothing shows on screen)

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

    def estimated_type_seconds(self, chars: int) -> float:
        """Rough wall-clock estimate for typing ``chars`` chars at current settings."""
        if chars <= 0:
            return 0.0
        # char_delay * mean tempo (~1.1) plus word/line/hesitation overhead.
        return chars * self.char_delay() * 1.4


CHOICES_PATH = CONFIG_PATH.parent / "choices.toml"  # written by `autotyper login`


def save_choices(values: dict[str, str], path: Path | None = None) -> None:
    """Remember login choices (provider, model). config.toml still overrides them."""
    p = path or CHOICES_PATH
    current: dict = {}
    if p.exists():
        try:
            current = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError:
            current = {}
    current.update(values)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Written by `autotyper login`. Settings in config.toml take priority."]
    lines += [f"{k} = {json.dumps(str(v))}" for k, v in current.items()]
    p.write_text("\n".join(lines) + "\n")


def load_settings(path: Path = CONFIG_PATH, choices: Path | None = None) -> Settings:
    s = Settings()
    cp = choices or (CHOICES_PATH if path == CONFIG_PATH else path.parent / "choices.toml")
    if cp.exists():
        try:
            apply_overrides(s, tomllib.loads(cp.read_text()))
        except tomllib.TOMLDecodeError:
            pass
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
provider = "claude"   # claude | ollama | gemini | openrouter | deepseek (autotyper login sets this)

[models]
orchestrator_model = "claude-opus-5-5"
orchestrator_effort = "xhigh"
worker_model = "claude-haiku-5-5"
worker_effort = "medium"
deepseek_model = "deepseek-chat"   # deepseek-chat | deepseek-reasoner
review = true

[typing]
wpm = 40
jitter = 0.45        # how much per-keystroke delay varies (log-normal spread)
line_pause = 0.5
word_pause = 0.12
hesitation = 0.03
hesitation_pause = 0.9
typo_rate = 0.02        # chance per character of a miskey, corrected right away
line_rewrite_rate = 0.0  # chance per line of deleting it and retyping it (off by default)
block_rewrite_rate = 0.05 # chance per method/class/XML element of a quick in-place formatting fix (never deletes the block)
format_rewrite_rate = 0.03 # chance per indented line of a mis-indent, corrected right away
repair = true
reformat = false
kotlin_only = true   # type only Kotlin source files and layout XML; skip values resources
time_limit_minutes = 30  # pace the whole run to finish within this many minutes
hud = false         # floating status overlay (opt-in; off by default)

[focus]
resume_idle = 1.2
auto_refocus = true

[keymap]   # JetBrains macOS keymap defaults
goto_line = "cmd+l"
select_to_doc_end = "cmd+shift+end"
"""
