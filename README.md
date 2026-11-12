# autotyper

**Describe an Android coding task. Claude writes the solution and autotyper types it into Android Studio, line by line, like a person would.**

Switch to your browser or another desktop and typing pauses. Come back to Android Studio and it picks up where it left off.

```text
$ autotyper solve "Add a settings screen with a dark-mode switch"

Project MyApp · Kotlin · Compose · 42 files read · task from --task text
╭─ Plan ───────────────────────────────────────────────────────────╮
│ New SettingsScreen with a dark-mode switch saved in DataStore    │
╰──────────────────────────────────────────────────────────────────╯
 #  File                                         Action  Lines
 1  app/src/main/java/com/example/SettingsScreen.kt   create   64
 2  app/src/main/java/com/example/MainActivity.kt     modify   48
Saved in .autotyper/session-20261111-101500. Read EXPLANATION.md there to see how the solution works.
Type 2 file(s) into Android Studio now? [Y/n]
```

Every solution also comes with an **EXPLANATION.md**: a plain-language walkthrough of what was written and why. Read it so you understand the code, not just have it.

---

## What you need

| | |
|---|---|
| **A Mac** | Typing uses macOS features. Other systems can generate the code but not type it. |
| **Android Studio** | Any recent version, with its default keyboard shortcuts. |
| **An Anthropic API key** | Get one at [console.anthropic.com](https://console.anthropic.com/settings/keys). Each task costs a few cents to a few dollars, depending on size. |
| **Python 3.10+** | Already on most Macs. Check with `python3 --version`. |

## Install

Pick one. Both install the `autotyper` command for your user.

**With pipx:**

```bash
brew install pipx && pipx ensurepath
```

```bash
pipx install git+https://github.com/TemaDeveloper/autotyper.git
```

**Or with uv:**

```bash
uv tool install git+https://github.com/TemaDeveloper/autotyper.git
```

Open a new terminal window afterwards so the command is found.

## Set up (once, about a minute)

```bash
autotyper setup
```

It walks you through four steps and opens the right settings for you:

1. **API key.** Paste it when asked. It is stored only on your Mac, in `~/.config/autotyper/credentials`, readable only by you.
2. **Android Studio.** Checks that it's installed.
3. **Accessibility permission** (required). Lets autotyper press keys. Turn it on for your terminal app (Terminal, iTerm, Warp…).
4. **Input Monitoring permission** (recommended). Lets autotyper notice when *you* start typing, so it can step aside, and enables the pause/stop hotkeys.

> macOS only applies a new permission after you **quit and reopen your terminal app**. If setup says "not active yet", do that and run `autotyper setup` again.

Not sure what's missing? Run `autotyper` on its own: it shows a ✔/✘ checklist and the next step.

## Use it

**1. Open a terminal in your Android project folder.**
Use a separate terminal app, *not* Android Studio's built-in terminal (keystrokes would land there).

```bash
cd ~/AndroidStudioProjects/MyApp
```

**2. Give it the task.** Any of these work:

```bash
autotyper solve
```

This asks you: type/paste the task, drag in a file, or use what you copied.

```bash
autotyper solve "Add a login screen that validates the email field"
```

```bash
autotyper solve task.pdf screenshot.png
```

```bash
autotyper solve --clipboard
```

Tasks can be text, PDF, screenshots (PNG/JPG), Markdown or a saved web page.

**3. Look at the plan and press Enter.** autotyper shows which files it will create or change.

**4. Click into Android Studio.** Typing starts on its own. While it runs:

| You do this | autotyper does this |
|---|---|
| Switch to the browser or another app | Pauses |
| Swipe to another desktop (Space) | Pauses |
| Open a different file tab | Pauses, then reopens the right file after a few seconds |
| Type or click yourself | Pauses until you stop for about a second |
| Come back to Android Studio | Checks the file, fixes anything that moved, continues |
| Press **Ctrl+Option+Cmd+P** | Pauses / resumes |
| Press **Ctrl+Option+Cmd+X** (or Ctrl+C in the terminal) | Stops |

**5. When it's done** you get a table of files and any next steps, such as "Sync Gradle" or "Run the app". Build and run the app to check it.

Stopped halfway, or want to watch it again? Saved solutions replay without calling Claude again:

```bash
autotyper type
```

That types the newest solution. Use `autotyper sessions` to list all of them.

## Commands

| Command | What it does |
|---|---|
| `autotyper` | Welcome screen: what's ready, what to do next |
| `autotyper setup` | Guided first-time setup |
| `autotyper solve [task or files]` | Write the solution and type it |
| `autotyper type [session]` | Type a saved solution again (no API cost) |
| `autotyper sessions` | List saved solutions for this project |
| `autotyper doctor` | Detailed check of everything; changes nothing |
| `autotyper --help` | All options |

Useful options for `solve` and `type`:

| Option | Meaning |
|---|---|
| `--instant` | Paste each file instead of typing it (fast) |
| `--wpm 150` | Typing speed in words per minute (default 110) |
| `--plan-only` | Write and save the solution but don't type it |
| `--project DIR` | Android project folder, if you're not in it |
| `-y` | Start typing without asking |
| `--no-review` | Skip the final review pass (faster, cheaper) |

## How it works

```text
 your task  +  your project's code
        │
        ▼
 Planner — Claude Opus 5.5      decides which files to create/change and exactly what goes in each
        │
        ├──► Writer — Claude Haiku 5.5   one per file, in parallel
        ├──► Writer — Claude Haiku 5.5
        └──► Writer — Claude Haiku 5.5
        │
        ▼
 Reviewer — Claude Opus 5.5     checks the files fit together (names, imports, signatures)
        │
        ▼
 .autotyper/session-…/          code, EXPLANATION.md, the plan
        │
        ▼
 typed into Android Studio
```

**Why typing into an IDE is tricky.** Android Studio "helps" while you type: it adds closing brackets and quotes, indents lines, and pops up suggestions. Typing code character by character would end up with doubled brackets and broken indentation. autotyper types one line at a time and clears whatever the editor added after the cursor before moving on. After each file it saves and compares the file on disk with the solution. If anything differs, it pastes the exact file, then puts back what was on your clipboard.

**Why pausing is safe.** Every time typing pauses, autotyper re-reads the file, keeps every line that is already correct, and continues from the first wrong one. A stray click that moved the cursor can't scramble the file.

## Settings

Defaults suit most people. To change them:

```bash
autotyper setup --write-config
```

Then edit `~/.config/autotyper/config.toml`:

```toml
[models]
orchestrator_model = "claude-opus-5-5"   # planner and reviewer
orchestrator_effort = "xhigh"            # low | medium | high | xhigh | max
worker_model = "claude-haiku-5-5"        # file writers
review = true

[typing]
wpm = 110          # typing speed
instant = false    # true = paste instead of type
repair = true      # paste the exact file if typing went wrong

[focus]
resume_idle = 1.2  # seconds you must be idle before typing resumes
auto_refocus = true

[keymap]           # only if you changed Android Studio's shortcuts
goto_line = "cmd+l"
select_to_doc_end = "cmd+shift+end"
```

Your API key can also come from the `ANTHROPIC_API_KEY` environment variable or `ant auth login` instead of the saved key.

## Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: autotyper` | Open a new terminal window. With pipx, run `pipx ensurepath` once. |
| "needs the Accessibility permission" even after allowing it | Quit and reopen your terminal app, then retry. |
| Wrong characters typed (e.g. Cyrillic) | Switch the keyboard layout to ABC or US while typing. `autotyper doctor` shows the current one. |
| "Not an Android project" | `cd` into the folder that has `settings.gradle` / `settings.gradle.kts`, or pass `--project`. |
| A file shows "differs - check this file" | Typing and the repair paste both failed. The correct code is in `.autotyper/session-…/files/`. |
| Hotkeys do nothing | Turn on Input Monitoring (`autotyper setup`), then reopen the terminal. |
| You changed Android Studio's keymap | Set the matching shortcuts under `[keymap]` in the settings file. |

## Privacy

Sent to Anthropic's API: the task, plus a limited selection of your project's source, Gradle and resource files. **Never sent:** `local.properties`, `google-services.json`, keystores, and build/IDE folders. Everything else stays on your Mac. Solutions are saved in `.autotyper/` inside your project; add `.autotyper/` to `.gitignore` if you don't want them committed.

## Use it honestly

autotyper is a learning and demo tool: see how a task can be solved, read the explanation, and watch the code come together in a real project. It runs in plain view in your terminal. Don't use it on exams, graded assignments or job interviews where AI help isn't allowed. That breaks most schools' and employers' rules.

## Limits

- **macOS only** for typing.
- **Generated code can be wrong.** A model checks it, not a compiler. Build, run and read it.
- **Up to 20 files** per task. Split bigger tasks.
- **Typing is human-paced on purpose.** Long files take a while; use `--instant` to paste.
- **Early release.** The typing engine is tested against a simulated Android Studio editor. Real-IDE testing is ongoing, so please [open an issue](https://github.com/TemaDeveloper/autotyper/issues) if something misbehaves.

## Development

```bash
git clone https://github.com/TemaDeveloper/autotyper.git && cd autotyper
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/python -m pytest -q
```

```text
src/autotyper/
├── cli.py               commands, setup wizard, prompts
├── credentials.py       API key lookup and private storage
├── config.py            settings and config file
├── project_context.py   reads the Android project for context
├── task_input.py        reads text, PDF, images, HTML, clipboard
├── solution.py          plan/files and saved sessions
├── llm/                 planner → writers → reviewer pipeline and prompts
├── typer/               typing engine, pause rules, simulated editor for tests
└── platform/macos.py    keystrokes, clipboard, window focus, hotkeys
```

The tests drive the typing engine against `typer/fake_editor.py`, a simulated JetBrains editor with auto-brackets, auto-indent and completion popups, so they never touch your real keyboard.
