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

    # --- models (Claude) ----------------------------------------------
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
    hud: bool = True  # floating status overlay (visible, also on screen shares)

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
provider = "claude"   # claude | ollama | gemini | openrouter (autotyper login sets this)

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
hud = true          # floating status overlay

[focus]
resume_idle = 1.2
auto_refocus = true

[keymap]   # JetBrains macOS keymap defaults
goto_line = "cmd+l"
select_to_doc_end = "cmd+shift+end"
"""
