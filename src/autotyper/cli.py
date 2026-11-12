"""autotyper command line.

  autotyper                      welcome screen: what to do next
  autotyper setup                guided first-time setup (API key, macOS permissions)
  autotyper solve [TASK|FILE...] solve a task and type it into Android Studio
  autotyper type [SESSION]       type a saved solution again (no API calls)
  autotyper sessions             list saved solutions
  autotyper doctor               check everything, change nothing
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import CONFIG_PATH, SAMPLE_CONFIG, Settings, load_settings
from .credentials import ant_cli, credential_source, looks_like_api_key, save_api_key
from .project_context import ProjectError, find_project_root, scan_project
from .solution import Solution, new_session_dir
from .task_input import TaskInputError, load_task, read_macos_clipboard

console = Console()

HOTKEYS = "[bold]Ctrl+Option+Cmd+P[/] pause/resume · [bold]Ctrl+Option+Cmd+X[/] (or Ctrl+C here) stop"
PANE_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PANE_INPUT_MONITORING = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
KEY_URL = "https://platform.claude.com/settings/keys"

EXAMPLES = """\
examples:
  autotyper setup                                   first-time setup (once)
  autotyper login                                   sign in to Claude with your browser
  autotyper solve                                   asks you for the task
  autotyper solve "Add a dark-mode switch to Settings"
  autotyper solve task.pdf screenshot.png           task from files
  autotyper solve --clipboard                       task you copied (text or screenshot)
  autotyper type                                    type the latest saved solution again
"""


# ---------------------------------------------------------------------- helpers
def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _terminal_name() -> str:
    prog = os.environ.get("TERM_PROGRAM", "")
    return {
        "Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "WarpTerminal": "Warp",
        "vscode": "Visual Studio Code", "ghostty": "Ghostty", "WezTerm": "WezTerm", "Hyper": "Hyper",
    }.get(prog, prog or "your terminal app")


def _inside_ide_terminal() -> bool:
    return "JetBrains" in os.environ.get("TERMINAL_EMULATOR", "")


def _settings(args) -> Settings:
    s = load_settings()
    for attr, flag in [
        ("orchestrator_model", "orchestrator_model"), ("orchestrator_effort", "effort"),
        ("worker_model", "worker_model"), ("wpm", "wpm"),
    ]:
        v = getattr(args, flag, None)
        if v is not None:
            setattr(s, attr, v)
    if getattr(args, "no_review", False):
        s.review = False
    if getattr(args, "instant", False):
        s.instant = True
    if getattr(args, "no_repair", False):
        s.repair = False
    if getattr(args, "reformat", False):
        s.reformat = True
    if getattr(args, "no_hud", False):
        s.hud = False
    return s


def _project_root(args) -> Path:
    start = Path(args.project).expanduser() if getattr(args, "project", None) else Path.cwd()
    return find_project_root(start)


def _confirm(question: str, default: bool = True) -> bool:
    if not _interactive():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    ans = console.input(f"{question} {suffix} ").strip().lower()
    return default if not ans else ans.startswith("y")


def _open_url(url: str) -> None:
    subprocess.run(["open", url], check=False, capture_output=True)


def _clean_path(raw: str) -> str:
    """A path typed or dragged into the terminal: strips quotes and backslash escapes."""
    raw = raw.strip()
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = [raw]
    return os.path.expanduser(" ".join(parts) if parts else raw)


def _split_positional(items: list[str]) -> tuple[list[str], str | None]:
    """Positional solve arguments: existing paths are files, everything else is task text."""
    files, words = [], []
    for item in items:
        p = _clean_path(item)
        (files if Path(p).is_file() else words).append(p if Path(p).is_file() else item)
    return files, (" ".join(words) or None)


def _print_plan(sol: Solution, issues: list[str]) -> None:
    console.print(Panel(sol.plan.summary, title="Plan", border_style="cyan"))
    t = Table(show_header=True, header_style="bold")
    t.add_column("#", justify="right")
    t.add_column("File")
    t.add_column("Action")
    t.add_column("Lines", justify="right")
    t.add_column("Purpose")
    for i, f in enumerate(sol.files, 1):
        purpose = next((p.purpose for p in sol.plan.files if p.path == f.path), "")
        t.add_row(str(i), f.path, f.action, str(f.content.count("\n")), purpose)
    console.print(t)
    if issues:
        console.print("[yellow]Review fixed:[/] " + "; ".join(issues))


# ---------------------------------------------------------------------- setup steps
def _browser_sign_in() -> bool:
    """Sign in through the browser with Anthropic's `ant` tool. Returns True when signed in."""
    if not ant_cli():
        if not shutil.which("brew"):
            console.print(
                "Browser sign-in uses Anthropic's [bold]ant[/] tool, which installs with Homebrew.\n"
                "Install Homebrew from https://brew.sh, or choose the API key option instead."
            )
            return False
        console.print("Browser sign-in uses Anthropic's free [bold]ant[/] tool. It installs with Homebrew:")
        console.print("  [dim]brew install anthropics/tap/ant[/]")
        if not _confirm("Install it now?", default=True):
            return False
        if subprocess.run(["brew", "install", "anthropics/tap/ant"], check=False).returncode != 0 or not ant_cli():
            console.print("[red]Installing ant didn't work.[/] Try the command above yourself, or use an API key.")
            return False
    console.print(
        "Your browser will open. Sign in to the Claude Console (Google sign-in works),\n"
        "pick your organization, then come back here."
    )
    subprocess.run([ant_cli(), "auth", "login"], check=False)
    if credential_source():
        console.print(f"[green]✔ Signed in[/] ({credential_source()}).")
        return True
    console.print("[yellow]Sign-in didn't finish.[/] Run [bold]autotyper login[/] to try again.")
    return False


def _connect_claude() -> bool:
    """Ask how to connect to Claude (browser sign-in or API key). Returns True when connected."""
    console.print(
        "autotyper needs access to Claude through your [bold]Claude Console[/] account\n"
        "(platform.claude.com). Usage is billed to that account's API credits; a claude.ai\n"
        "Pro/Max chat subscription can't be used by third-party tools."
    )
    console.print("  [bold cyan]1[/]  Sign in with your browser (Google or email) [dim]- recommended[/]")
    console.print("  [bold cyan]2[/]  Paste an API key")
    choice = console.input("Choose [bold]1[/] or [bold]2[/] (Enter = 1, s = skip): ").strip().lower()
    if choice == "s":
        return False
    if choice == "2":
        return _ask_api_key()
    return _browser_sign_in()


def _ask_api_key() -> bool:
    """Prompt for an API key and save it. Returns True if one was saved."""
    console.print(
        "autotyper uses Claude, so it needs an Anthropic API key.\n"
        f"Create one at [link={KEY_URL}]{KEY_URL}[/link] (it starts with [bold]sk-ant-[/])."
    )
    if _confirm("Open that page in your browser?", default=True):
        _open_url(KEY_URL)
    for _ in range(3):
        key = getpass.getpass("Paste your API key (hidden) and press Enter, or just Enter to skip: ").strip()
        if not key:
            return False
        if looks_like_api_key(key):
            path = save_api_key(key)
            console.print(f"[green]✔ Saved[/] to {path} (only your user can read it).")
            return True
        console.print("[red]That doesn't look like an Anthropic API key[/] (it should start with sk-ant-).")
    return False


def _wait_for_permission(name: str, pane: str, check, why: str) -> bool:
    term = _terminal_name()
    console.print(
        f"[bold]{name}[/] lets autotyper {why}.\n"
        f"In the window that opens, turn on [bold]{term}[/] "
        f"(if it isn't listed, click [bold]+[/] and add it from Applications)."
    )
    _open_url(pane)
    if not _interactive():
        return check()
    console.input("Press [bold]Enter[/] when you've turned it on... ")
    if check():
        return True
    console.print(
        f"[yellow]Not active yet.[/] macOS applies this after you [bold]quit and reopen {term}[/]. "
        "Do that, then run [bold]autotyper setup[/] again."
    )
    return False


# ---------------------------------------------------------------------- typing
def _type_solution(sol: Solution, settings: Settings, only: list[str] | None = None) -> int:
    if sys.platform != "darwin":
        console.print("[red]Typing works on macOS only.[/] The generated files are in the session folder.")
        return 2
    from .platform import macos
    from .typer.engine import TypingEngine
    from .typer.gate import Aborted, FocusGate, NullMonitor

    if _inside_ide_terminal():
        console.print(
            "[red]You're running autotyper inside Android Studio's own terminal.[/]\n"
            "Keystrokes would land in that terminal. Open Terminal (or iTerm/Warp) and run it there."
        )
        return 2
    if not macos.accessibility_trusted(prompt=False):
        console.print("[red]autotyper can't type yet:[/] it needs the Accessibility permission.")
        if not (_interactive() and _wait_for_permission(
            "Accessibility", PANE_ACCESSIBILITY, macos.accessibility_trusted, "press keys in Android Studio",
        )):
            console.print("Run [bold]autotyper setup[/] to fix it, then [bold]autotyper type[/] to continue.")
            return 3
    app = macos.find_ide_app(settings.ide_bundle_ids, settings.ide_app_path)
    if not app:
        console.print(
            "[red]Android Studio wasn't found.[/] Install it from https://developer.android.com/studio, "
            f"or set ide_app_path in {CONFIG_PATH}."
        )
        return 3

    monitor = macos.MacInputMonitor()
    if not (macos.input_monitoring_allowed() and monitor.start()):
        console.print(
            "[yellow]Input Monitoring is off,[/] so the hotkeys and 'pause while you type' won't work. "
            "Typing still pauses when you leave Android Studio. ([bold]autotyper setup[/] turns it on.)"
        )
        monitor = NullMonitor()

    from .platform.hud import HudClient

    hud = HudClient() if settings.hud else None
    status_line = {"msg": ""}

    def on_status(state: str, msg: str) -> None:
        if hud:
            hud.update(state=state, msg=msg)
        style = {"waiting": "yellow", "typing": "green", "info": "cyan"}.get(state, "white")
        icon = {"waiting": "⏸", "typing": "▶", "info": "•"}.get(state, "•")
        if msg != status_line["msg"]:
            console.print(f"[{style}]{icon} {msg}[/]")
            status_line["msg"] = msg

    opener = macos.MacFileOpener(app)
    gate = FocusGate(
        macos.MacFocusProbe(), monitor, settings.ide_bundle_ids,
        resume_idle=settings.resume_idle, poll_interval=settings.poll_interval,
        auto_refocus=settings.auto_refocus, refocus_after=settings.refocus_after,
        on_status=on_status, on_refocus=opener.open_file,
    )
    root = Path(sol.project_root)
    files = [f for f in sol.files if not only or f.path in only]
    progress = {"path": None, "n": 0}

    def on_progress(path: str, line: int, total: int) -> None:
        if progress["path"] != path:
            progress["path"] = path
            progress["n"] += 1
            console.print(f"[green]⌨  [{progress['n']}/{len(files)}] {Path(path).relative_to(root)}[/] ({total} lines)")
        if hud:
            hud.update(file=Path(path).name, index=progress["n"], count=len(files), line=line, total=total)

    engine = TypingEngine(macos.MacKeyboard(), macos.MacClipboard(), opener, gate, settings, on_progress=on_progress)

    console.print(Panel(
        "1. Open your project in Android Studio.\n"
        "2. Click into the Android Studio window. Typing starts by itself.\n"
        "3. Need to look something up? Just switch away. Typing pauses and picks up where it was when you come back.\n"
        "Don't type in the editor while it runs.\n" + HOTKEYS,
        title="Ready to type", border_style="green",
    ))
    results = []
    try:
        for f in files:
            results.append(engine.type_file(str(root / f.path), f.content))
    except (Aborted, KeyboardInterrupt):
        console.print("[red]Stopped.[/] Run [bold]autotyper type[/] to continue where it left off.")
        if hud:
            hud.update(state="stopped", msg="Run autotyper type to continue")
            hud.close(linger=2.0)
        return 130
    finally:
        if hasattr(monitor, "stop"):
            monitor.stop()
    if hud:
        bad = sum(r.status == "mismatch" for r in results)
        hud.update(state="done", msg=f"{len(results)} file(s) written" + (f", {bad} to check" if bad else ""))
        hud.close(linger=4.0)

    t = Table(title="Done", header_style="bold")
    t.add_column("File")
    t.add_column("Result")
    t.add_column("Pauses", justify="right")
    labels = {
        "typed": "[green]typed[/]", "repaired": "[yellow]typed, then fixed by paste[/]",
        "unchanged": "[dim]already correct[/]", "mismatch": "[red]differs - check this file[/]",
    }
    for r in results:
        t.add_row(str(Path(r.path).relative_to(root)), labels.get(r.status, r.status), str(r.resyncs))
    console.print(t)
    if sol.plan.user_steps:
        console.print("[bold]What to do next:[/]")
        for step in sol.plan.user_steps:
            console.print(f"  • {step}")
    return 0 if all(r.status != "mismatch" for r in results) else 1


# ---------------------------------------------------------------------- task prompt
def _prompt_task() -> tuple[str | None, list[str], bool]:
    """Ask how the task should be given. Returns (text, files, use_clipboard)."""
    clip_text, clip_png = (None, None)
    try:
        clip_text, clip_png = read_macos_clipboard()
    except Exception:  # noqa: BLE001 - clipboard is optional here
        pass
    clip_desc = (
        "a screenshot" if clip_png else
        f"text: \"{' '.join(clip_text.split())[:50]}…\"" if clip_text and clip_text.strip() else None
    )
    console.print("[bold]What's the task?[/]")
    console.print("  [bold cyan]1[/]  Type or paste it here")
    console.print("  [bold cyan]2[/]  A file: PDF, screenshot, text or web page")
    if clip_desc:
        console.print(f"  [bold cyan]3[/]  What you copied ({clip_desc})")
    choice = console.input("Choose [bold]1[/], [bold]2[/]" + (" or [bold]3[/]" if clip_desc else "") + ": ").strip()

    if choice == "2":
        files = []
        console.print("Drag the file into this window (or type its path), then press Enter. Empty line when done.")
        while True:
            raw = console.input("  file: ").strip()
            if not raw:
                break
            path = _clean_path(raw)
            if Path(path).is_file():
                files.append(path)
                console.print(f"  [green]✔[/] {Path(path).name}")
            else:
                console.print(f"  [red]Not found:[/] {path}")
        return None, files, False
    if choice == "3" and clip_desc:
        return None, [], True

    console.print("Paste the task. When finished, type [bold]END[/] on its own line (or press Ctrl+D).")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines), [], False


def _ensure_credentials() -> bool:
    if credential_source():
        return True
    console.print("[yellow]autotyper isn't connected to Claude yet.[/]")
    if _interactive() and _connect_claude():
        return True
    console.print("Run [bold]autotyper login[/] to connect.")
    return False


# ---------------------------------------------------------------------- commands
def cmd_welcome(args) -> int:
    console.print(Panel(
        "Give it a coding task. Claude writes the solution for your Android project\n"
        "and autotyper types it into Android Studio. Switch to another app or desktop\n"
        "and typing pauses. Come back and it continues.",
        title=f"autotyper {__version__}", border_style="cyan",
    ))
    ready = True
    if sys.platform == "darwin":
        from .platform import macos

        checks = [
            ("Connected to Claude", bool(credential_source())),
            ("Accessibility permission", macos.accessibility_trusted()),
            ("Android Studio installed", bool(macos.find_ide_app(load_settings().ide_bundle_ids, load_settings().ide_app_path))),
        ]
        for name, good in checks:
            console.print(f"  {'[green]✔[/]' if good else '[red]✘[/]'} {name}")
            ready = ready and good
    if not ready:
        console.print("\nNext: run [bold]autotyper setup[/] (takes about a minute).")
    else:
        console.print("\nNext: [bold]cd[/] into your Android project and run [bold]autotyper solve[/].")
    console.print("\n[dim]All commands: autotyper --help[/]")
    return 0


def cmd_setup(args) -> int:
    console.print(Panel("Takes about a minute. You can run it again any time.", title="autotyper setup", border_style="cyan"))
    if sys.platform != "darwin":
        console.print("[yellow]Typing works on macOS only.[/] On this system autotyper can still generate the files.")
    settings = load_settings()
    all_ok = True

    console.print("\n[bold]Step 1 of 4 · Connect to Claude[/]")
    src = credential_source()
    if src:
        console.print(f"[green]✔[/] Connected ({src}).")
    elif not _connect_claude():
        console.print("[yellow]Not connected yet.[/] Run [bold]autotyper login[/] any time.")
        all_ok = False

    if sys.platform == "darwin":
        from .platform import macos

        console.print("\n[bold]Step 2 of 4 · Android Studio[/]")
        app = macos.find_ide_app(settings.ide_bundle_ids, settings.ide_app_path)
        if app:
            console.print(f"[green]✔[/] {app}")
        else:
            console.print("[red]✘ Not found.[/] Install it from https://developer.android.com/studio")
            all_ok = False

        console.print("\n[bold]Step 3 of 4 · Accessibility permission[/] (required)")
        if macos.accessibility_trusted():
            console.print("[green]✔[/] Allowed.")
        else:
            macos.accessibility_trusted(prompt=True)
            all_ok &= _wait_for_permission(
                "Accessibility", PANE_ACCESSIBILITY, macos.accessibility_trusted, "press keys in Android Studio",
            )

        console.print("\n[bold]Step 4 of 4 · Input Monitoring permission[/] (recommended)")
        if macos.input_monitoring_allowed():
            console.print("[green]✔[/] Allowed.")
        else:
            macos.input_monitoring_allowed(request=True)
            if not _wait_for_permission(
                "Input Monitoring", PANE_INPUT_MONITORING, macos.input_monitoring_allowed,
                "notice your own typing (to pause) and enable the pause/stop hotkeys",
            ):
                console.print("[dim]Optional: autotyper still works without it, just without hotkeys.[/]")

    if args.write_config and not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(SAMPLE_CONFIG)
        console.print(f"\nWrote settings file {CONFIG_PATH}")

    if all_ok:
        console.print(Panel(
            "Open your Android project in a terminal and run:\n\n  [bold]autotyper solve[/]",
            title="[green]You're all set[/]", border_style="green",
        ))
        return 0
    console.print("\n[yellow]Almost there.[/] Fix the items above, then run [bold]autotyper setup[/] again.")
    return 1


def cmd_login(args) -> int:
    src = credential_source()
    if src and not args.again:
        console.print(f"[green]✔ Already connected[/] ({src}). Use [bold]autotyper login --again[/] to switch account.")
        return 0
    return 0 if _connect_claude() else 1


def cmd_solve(args) -> int:
    settings = _settings(args)
    root = _project_root(args)
    if not _ensure_credentials():
        return 4

    pos_files, pos_text = _split_positional(args.task_args or [])
    text = "\n\n".join(t for t in (args.task, pos_text) if t) or None
    files = (args.file or []) + pos_files
    stdin_text = None
    use_clipboard = args.clipboard
    if not (text or files or use_clipboard):
        if _interactive():
            text, files, use_clipboard = _prompt_task()
        else:
            stdin_text = sys.stdin.read()
    task = load_task(text=text, files=files, clipboard=use_clipboard, stdin_text=stdin_text)
    ctx = scan_project(root)
    console.print(
        f"Project [bold]{root.name}[/] · {ctx.language} · {'Compose' if ctx.uses_compose else 'Views'} · "
        f"{len(ctx.files)} files read · task from {', '.join(task.sources)}"
    )

    from .llm.pipeline import Pipeline

    with console.status("Thinking about the task... (usually 1-3 minutes)") as status:
        pipe = Pipeline(settings, on_status=lambda m: status.update(m))
        solution, issues = asyncio.run(pipe.solve(task, ctx))
    session = solution.save(new_session_dir(root))
    _print_plan(solution, issues)
    rel = session.relative_to(root)
    console.print(f"Saved in [bold]{rel}[/]. Read [bold]EXPLANATION.md[/] there to see how the solution works.")
    for model, u in pipe.usage.items():
        console.print(f"[dim]{model}: {u['input']:,} in / {u['output']:,} out tokens ({u['cache_read']:,} cached)[/]")

    if args.plan_only:
        console.print("Type it later with: [bold]autotyper type[/]")
        return 0
    if not args.yes and not _confirm(f"Type {len(solution.files)} file(s) into Android Studio now?"):
        console.print("Type it later with: [bold]autotyper type[/]")
        return 0
    return _type_solution(solution, settings)


def _latest_session(root: Path) -> Path | None:
    base = root / ".autotyper"
    sessions = sorted(base.glob("session-*"), reverse=True) if base.exists() else []
    return sessions[0] if sessions else None


def cmd_type(args) -> int:
    settings = _settings(args)
    if args.session:
        session = Path(_clean_path(args.session)).resolve()
    else:
        session = _latest_session(_project_root(args))
        if not session:
            console.print("No saved solutions in this project yet. Run [bold]autotyper solve[/] first.")
            return 2
        console.print(f"Using the latest solution: [bold]{session.name}[/]")
    if not (session / "solution.json").exists():
        console.print(f"[red]{session} is not a saved autotyper solution.[/] See [bold]autotyper sessions[/].")
        return 2
    sol = Solution.load(session)
    if args.project:
        sol.project_root = str(_project_root(args))
    _print_plan(sol, [])
    if not args.yes and not _confirm(f"Type {len(sol.files)} file(s) into Android Studio now?"):
        return 0
    return _type_solution(sol, settings, only=args.only)


def cmd_sessions(args) -> int:
    root = _project_root(args)
    base = root / ".autotyper"
    sessions = sorted(base.glob("session-*"), reverse=True) if base.exists() else []
    if not sessions:
        console.print("No saved solutions yet. Run [bold]autotyper solve[/].")
        return 0
    t = Table(header_style="bold")
    t.add_column("Session")
    t.add_column("Files", justify="right")
    t.add_column("Summary")
    for s in sessions:
        try:
            sol = Solution.load(s)
            t.add_row(str(s.relative_to(root)), str(len(sol.files)), sol.plan.summary[:90])
        except Exception:  # noqa: BLE001 - listing must not crash on a broken folder
            t.add_row(str(s.relative_to(root)), "?", "[red]unreadable[/]")
    console.print(t)
    console.print("[dim]Type one with: autotyper type <session>   (no session = the newest)[/]")
    return 0


def cmd_doctor(args) -> int:
    ok = True

    def row(good: bool | None, name: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "[green]✔[/]", False: "[red]✘[/]", None: "[yellow]![/]"}[good]
        if good is False:
            ok = False
        console.print(f"{mark} {name}" + (f" - {detail}" if detail else ""))

    settings = load_settings()
    row(sys.platform == "darwin", "macOS", sys.platform)
    row(sys.version_info >= (3, 10), "Python", sys.version.split()[0])
    src = credential_source()
    row(True if src else False, "Connected to Claude", src or "no - run autotyper login")
    row(None if not CONFIG_PATH.exists() else True, "Settings file",
        str(CONFIG_PATH) if CONFIG_PATH.exists() else "using defaults (autotyper setup --write-config creates one)")
    console.print(f"  models: planner [bold]{settings.orchestrator_model}[/] @ {settings.orchestrator_effort}, "
                  f"writers [bold]{settings.worker_model}[/]")

    if sys.platform == "darwin":
        from .platform import macos

        app = macos.find_ide_app(settings.ide_bundle_ids, settings.ide_app_path)
        row(bool(app), "Android Studio", app or "not found")
        row(macos.accessibility_trusted(), "Accessibility permission", f"for {_terminal_name()}: needed to type")
        row(macos.input_monitoring_allowed() or None, "Input Monitoring permission",
            "optional: hotkeys and pause-while-you-type")
        row(not _inside_ide_terminal() or None, "Terminal",
            "Android Studio's built-in terminal - use a separate terminal app" if _inside_ide_terminal() else _terminal_name())
        try:
            layout = subprocess.run(["defaults", "read", "com.apple.HIToolbox", "AppleCurrentKeyboardLayoutInputSourceID"],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            layout = ""
        latin = any(x in layout for x in ("ABC", "US", "British", "USInternational", "Dvorak", "Colemak"))
        row(True if latin else None, "Keyboard layout", (layout or "unknown") + ("" if latin else " - switch to ABC/US while typing"))
    try:
        root = _project_root(args)
        ctx = scan_project(root)
        row(True, "Project", f"{root} ({ctx.language}, {'Compose' if ctx.uses_compose else 'Views'}, namespace {ctx.namespace})")
    except ProjectError as e:
        row(None, "Project", f"{e} (cd into your Android project or pass --project)")

    if not ok:
        console.print("\nRun [bold]autotyper setup[/] to fix the ✘ items.")
    return 0 if ok else 1


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autotyper",
        description="Solve an Android coding task with Claude and type the solution into Android Studio.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"autotyper {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def typing_flags(sp):
        g = sp.add_argument_group("typing")
        g.add_argument("--project", metavar="DIR", help="Android project folder (default: this folder)")
        g.add_argument("--wpm", type=int, help="typing speed in words per minute (default 110)")
        g.add_argument("--instant", action="store_true", help="paste each file instead of typing it")
        g.add_argument("--no-repair", action="store_true", help="don't paste the exact file if typing went wrong")
        g.add_argument("--reformat", action="store_true", help="run Reformat Code after each file")
        g.add_argument("--no-hud", action="store_true", help="don't show the floating status overlay")
        g.add_argument("-y", "--yes", action="store_true", help="start typing without asking")

    st = sub.add_parser("setup", help="first-time setup: Claude sign-in and macOS permissions")
    st.add_argument("--write-config", action="store_true", help="also create a settings file to customise")
    st.set_defaults(func=cmd_setup)

    lg = sub.add_parser("login", help="connect to Claude: browser sign-in or API key")
    lg.add_argument("--again", action="store_true", help="sign in again, e.g. with another account")
    lg.set_defaults(func=cmd_login)

    s = sub.add_parser(
        "solve", help="solve a task and type it into Android Studio",
        description="Solve a task and type the solution into Android Studio. "
                    "With no task given, autotyper asks for one.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("task_args", nargs="*", metavar="TASK", help="task text, or task files (PDF, image, .txt, .md, .html)")
    s.add_argument("--task", "-t", help="task text")
    s.add_argument("--file", "-f", action="append", help="task file (repeatable)")
    s.add_argument("--clipboard", "-c", action="store_true", help="use the task you copied (text or screenshot)")
    s.add_argument("--plan-only", action="store_true", help="write and save the solution, don't type it")
    a = s.add_argument_group("models")
    a.add_argument("--no-review", action="store_true", help="skip the final review pass (faster, cheaper)")
    a.add_argument("--orchestrator-model", metavar="MODEL", help="planner/reviewer model (default claude-opus-5-5)")
    a.add_argument("--worker-model", metavar="MODEL", help="file-writer model (default claude-haiku-5-5)")
    a.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="planner effort (default xhigh)")
    typing_flags(s)
    s.set_defaults(func=cmd_solve)

    t = sub.add_parser("type", help="type a saved solution again (no API calls)")
    t.add_argument("session", nargs="?", help="session folder (default: the newest one)")
    t.add_argument("--only", action="append", metavar="PATH", help="type only this file (repeatable)")
    typing_flags(t)
    t.set_defaults(func=cmd_type)

    ls = sub.add_parser("sessions", help="list saved solutions for this project")
    ls.add_argument("--project", metavar="DIR")
    ls.set_defaults(func=cmd_sessions)

    d = sub.add_parser("doctor", help="check everything and report (changes nothing)")
    d.add_argument("--project", metavar="DIR")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_welcome
    try:
        return args.func(args)
    except TaskInputError as e:
        console.print(f"[red]Problem with the task:[/] {e}")
        return 2
    except ProjectError as e:
        console.print(f"[red]{e}[/]\n[dim]Run autotyper from inside your Android project folder, or add --project <folder>.[/]")
        return 2
    except (KeyboardInterrupt, EOFError):
        console.print("\n[red]Cancelled.[/]")
        return 130
    except Exception as e:  # noqa: BLE001
        from .llm.pipeline import LLMError

        if isinstance(e, LLMError):
            console.print(f"[red]{e}[/]")
            return 4
        raise


if __name__ == "__main__":
    sys.exit(main())
