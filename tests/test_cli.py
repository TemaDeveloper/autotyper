import os
import stat

import pytest

from autotyper import cli, credentials


def test_split_positional_files_and_text(tmp_path):
    f = tmp_path / "My Task.pdf"
    f.write_bytes(b"%PDF")
    files, text = cli._split_positional([str(f), "Add", "a", "button"])
    assert files == [str(f)]
    assert text == "Add a button"


def test_clean_path_handles_dragged_escapes(tmp_path):
    f = tmp_path / "My Task.pdf"
    f.write_bytes(b"x")
    dragged = str(f).replace(" ", "\\ ") + " "
    assert cli._clean_path(dragged) == str(f)
    assert cli._clean_path(f"'{f}'") == str(f)


def test_save_api_key_is_private(tmp_path):
    p = credentials.save_api_key("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", tmp_path / "credentials")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert credentials.saved_api_key(p) == "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"


@pytest.mark.parametrize("key,ok", [
    ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", True),
    ("sk-ant-short", False),
    ("hello world", False),
    ("sk-ant-api03-abc def ghijklmnopqrstuvwxyz", False),
])
def test_looks_like_api_key(key, ok):
    assert credentials.looks_like_api_key(key) is ok


def test_client_kwargs_prefers_env(monkeypatch, tmp_path):
    p = tmp_path / "credentials"
    credentials.save_api_key("sk-ant-api03-savedsavedsavedsaved", p)
    monkeypatch.setattr(credentials, "CREDENTIALS_PATH", p)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert credentials.client_kwargs() == {"api_key": "sk-ant-api03-savedsavedsavedsaved"}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert credentials.client_kwargs() == {}


def test_parser_accepts_positional_task_and_optional_session():
    p = cli.build_parser()
    a = p.parse_args(["solve", "Add", "login", "--instant"])
    assert a.task_args == ["Add", "login"] and a.instant
    b = p.parse_args(["type"])
    assert b.session is None
    c = p.parse_args([])
    assert c.cmd is None


def test_type_without_sessions_is_friendly(tmp_path, capsys):
    (tmp_path / "settings.gradle.kts").write_text('include(":app")\n')
    code = cli.main(["type", "--project", str(tmp_path)])
    assert code == 2
    assert "autotyper solve" in capsys.readouterr().out


def test_browser_sign_in_profile_detected(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_PROFILE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "CREDENTIALS_PATH", tmp_path / "none")
    assert credentials.credential_source() is None
    (tmp_path / "credentials").mkdir()
    (tmp_path / "credentials" / "work.json").write_text("{}")
    (tmp_path / "active_config").write_text("work\n")
    assert credentials.signed_in_profile() == "work"
    assert "browser sign-in" in credentials.credential_source()
    assert credentials.client_kwargs() == {}  # bare client -> SDK uses the profile


def test_login_claude_when_already_connected(monkeypatch, capsys):
    saved = {}
    monkeypatch.setattr(cli, "credential_source", lambda: "browser sign-in (profile default)")
    monkeypatch.setattr(cli, "save_choices", lambda v: saved.update(v))
    assert cli.main(["login", "claude"]) == 0
    assert "Using Claude" in capsys.readouterr().out
    assert saved == {"provider": "claude"}
