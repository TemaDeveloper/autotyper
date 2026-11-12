"""Anthropic API key lookup and storage.

Order: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN env vars, then the key saved
by `autotyper setup` in ~/.config/autotyper/credentials (mode 0600), then the
`ant` CLI login (picked up by the SDK itself).
"""

from __future__ import annotations

import os
import subprocess
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


def credential_source() -> str | None:
    """Where credentials come from, or None if there are none."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY"
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN"
    if saved_api_key():
        return str(CREDENTIALS_PATH)
    try:
        if subprocess.run(["ant", "auth", "status"], capture_output=True, timeout=10).returncode == 0:
            return "ant auth login"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def client_kwargs() -> dict:
    """Extra AsyncAnthropic kwargs: the saved key, only when no env var is set."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return {}
    key = saved_api_key()
    return {"api_key": key} if key else {}
