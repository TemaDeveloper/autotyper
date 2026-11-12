import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autotyper.config import Settings
from autotyper.llm.pipeline import LLMError, Pipeline, safe_relpath
from autotyper.project_context import scan_project
from autotyper.solution import Solution
from autotyper.task_input import TaskInput


def _msg(payload, stop="end_turn"):
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=None,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, responder):
        self.calls = []
        outer = self

        class Messages:
            def stream(self, **kw):
                outer.calls.append(kw)
                return FakeStream(responder(kw))

        self.beta = SimpleNamespace(messages=Messages())


PLAN = {
    "summary": "Counter app",
    "approach": "Button increments a TextView",
    "files": [
        {"path": "app/src/main/java/com/ex/MainActivity.kt", "action": "modify", "purpose": "logic", "spec": "count taps", "order": 2},
        {"path": "app/src/main/res/layout/activity_main.xml", "action": "create", "purpose": "ui", "spec": "button + label", "order": 1},
    ],
    "user_steps": ["Run the app"],
    "explanation": "# How it works",
}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "settings.gradle.kts").write_text('include(":app")\n')
    src = tmp_path / "app/src/main/java/com/ex"
    src.mkdir(parents=True)
    (src / "MainActivity.kt").write_text("package com.ex\n\nclass MainActivity\n")
    (tmp_path / "app/build.gradle.kts").write_text('android { namespace = "com.ex" }\n')
    return tmp_path


def test_solve_runs_plan_workers_review(project):
    def respond(kw):
        system = kw["system"]
        if "lead Android engineer of a coding assistant" in system:
            return _msg(PLAN)
        if "writing exactly ONE file" in system:
            ask = kw["messages"][0]["content"][-1]["text"]
            name = "xml" if "activity_main.xml" in ask else "kt"
            return _msg({"content": f"```\n// {name}\n```", "notes": ""})
        return _msg({"verdict": "fixed", "issues": ["missing import"],
                     "files": [{"path": "app/src/main/java/com/ex/MainActivity.kt", "content": "// fixed kt"}]})

    client = FakeClient(respond)
    pipe = Pipeline(Settings(), client=client)
    sol, issues = asyncio.run(pipe.solve(TaskInput("make a counter", [], ["t"]), scan_project(project)))

    assert [f.path for f in sol.files] == [
        "app/src/main/res/layout/activity_main.xml",
        "app/src/main/java/com/ex/MainActivity.kt",
    ]
    assert sol.files[0].content == "// xml\n"  # fences stripped
    assert sol.files[1].content == "// fixed kt\n"  # review fix applied
    assert sol.files[1].action == "modify" and "class MainActivity" in sol.files[1].original
    assert issues == ["missing import"]

    models = [c["model"] for c in client.calls]
    assert models[0] == "claude-opus-5-5" and models[-1] == "claude-opus-5-5"
    assert models[1:3] == ["claude-haiku-5-5"] * 2
    opus = client.calls[0]
    assert opus["output_config"]["effort"] == "xhigh"
    assert opus["fallbacks"] == "default" and opus["betas"] == ["server-side-fallback-2026-07-01"]
    assert "fallbacks" not in client.calls[1]  # Haiku has no server-side fallback
    assert opus["output_config"]["format"]["type"] == "json_schema"

    # round-trips through the session folder
    out = sol.save(project / ".autotyper" / "s1")
    again = Solution.load(out)
    assert again.files[1].content == "// fixed kt\n"
    assert (out / "EXPLANATION.md").read_text() == "# How it works"
    assert (out / "files/app/src/main/res/layout/activity_main.xml").exists()


def test_refusal_raises(project):
    pipe = Pipeline(Settings(), client=FakeClient(lambda kw: _msg({}, stop="refusal")))
    with pytest.raises(LLMError, match="declined"):
        asyncio.run(pipe.plan(TaskInput("x", [], ["t"]), scan_project(project)))


@pytest.mark.parametrize("bad", ["../evil.kt", "/etc/passwd", "app/build/x.kt", ".idea/a.xml", "local.properties"])
def test_safe_relpath_rejects(tmp_path, bad):
    with pytest.raises(LLMError):
        safe_relpath(tmp_path, bad)


def test_safe_relpath_normalizes(tmp_path):
    assert safe_relpath(tmp_path, "./app\\src\\A.kt") == "app/src/A.kt"
