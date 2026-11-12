"""Anthropic API key lookup and storage.

Order: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN env vars, then the key saved
by `autotyper setup` in ~/.config/autotyper/credentials (mode 0600), then a
browser sign-in made with `ant auth login` (the Anthropic SDK reads that
profile from ~/.config/anthropic by itself).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import CONFIG_PATH

CREDENTIALS_PATH = Path(
    os.environ.get("AUTOTYPER_CREDENTIALS", str(CONFIG_PATH.parent / "credentials"))
).expanduser()


def saved_api_key(path: Path | None = None) -> str | None:
    p = path or CREDENTIALS_PATH
    try:
        key = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def save_api_key(key: str, path: Path | None = None) -> Path:
    p = path or CREDENTIALS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key.strip() + "\n")
    os.chmod(p, 0o600)
    return p


def looks_like_api_key(key: str) -> bool:
    key = key.strip()
    return key.startswith("sk-ant-") and len(key) > 20 and not any(c.isspace() for c in key)


def anthropic_config_dir() -> Path:
    return Path(os.environ.get("ANTHROPIC_CONFIG_DIR", "~/.config/anthropic")).expanduser()


def signed_in_profile() -> str | None:
    """Name of the `ant auth login` profile the SDK will use, if it has credentials on disk."""
    base = anthropic_config_dir()
    name = os.environ.get("ANTHROPIC_PROFILE")
    if not name:
        try:
            name = (base / "active_config").read_text(encoding="utf-8").strip() or "default"
        except OSError:
            name = "default"
    return name if (base / "credentials" / f"{name}.json").is_file() else None


def ant_cli() -> str | None:
    return shutil.which("ant")


def credential_source() -> str | None:
    """Where credentials come from, or None if there are none."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN"
    if saved_api_key():
        return f"API key in {CREDENTIALS_PATH}"
    profile = signed_in_profile()
    if profile:
        return f"browser sign-in (profile {profile})"
    return None


def client_kwargs() -> dict:
    """Extra AsyncAnthropic kwargs: the saved key, only when no env var is set."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {}
    key = saved_api_key()
    return {"api_key": key} if key else {}
