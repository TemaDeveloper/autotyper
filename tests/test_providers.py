import asyncio
import json

import httpx
import pytest

from autotyper import providers
from autotyper.config import Settings, load_settings, save_choices
from autotyper.llm.backends import BadJSON, LLMError, OllamaBackend, OpenAICompatBackend, extract_json
from autotyper.llm.pipeline import Pipeline
from autotyper.project_context import scan_project
from autotyper.task_input import TaskInput

SCHEMA = {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}


def run(coro):
    return asyncio.run(coro)


def call(backend, content=None, schema=SCHEMA):
    return run(backend.complete(model="m", effort="xhigh", system="sys", content=content or [{"type": "text", "text": "hi"}],
                                schema=schema, max_tokens=100, label="test"))


@pytest.mark.parametrize("text", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here it is:\n{"a": 1}\nThanks'])
def test_extract_json_tolerates_wrapping(text):
    assert extract_json(text) == {"a": 1}


def test_openai_compat_downgrades_format_and_parses():
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body.get("response_format", {}).get("type"))
        assert req.headers["authorization"] == "Bearer k"
        assert body["reasoning_effort"] == "high"  # xhigh maps to Gemini's highest
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "unsupported schema"}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"content": "x"}'}}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    b = OpenAICompatBackend("gemini", "https://g.test/v1", "k", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    data, usage = call(b)
    assert data == {"content": "x"} and usage["input"] == 7
    assert seen == ["json_schema", "json_object"]


def test_openai_compat_retries_rate_limit_then_explains():
    waits = []

    async def fake_sleep(s):
        waits.append(s)

    b = OpenAICompatBackend("openrouter", "https://o.test/v1", "k", sleep=fake_sleep, max_retries=2,
                            http=httpx.AsyncClient(transport=httpx.MockTransport(
                                lambda r: httpx.Response(429, headers={"retry-after": "3"}))))
    with pytest.raises(LLMError, match="free-tier limit"):
        call(b)
    assert waits == [3.0, 3.0]


def test_openai_compat_sends_images_as_data_urls():
    def handler(req):
        user = json.loads(req.content)["messages"][1]["content"]
        assert user[1]["image_url"]["url"].startswith("data:image/png;base64,AAAA")
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"content": "ok"}'}}]})

    b = OpenAICompatBackend("gemini", "https://g.test", "k", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
               {"type": "text", "text": "task"}]
    assert call(b, content)[0] == {"content": "ok"}


def test_ollama_streams_and_sets_context_window():
    def handler(req):
        body = json.loads(req.content)
        assert body["format"] == SCHEMA and body["options"]["num_ctx"] == 32768
        lines = [{"message": {"content": '{"content": '}}, {"message": {"content": '"hi"}'}},
                 {"done": True, "done_reason": "stop", "prompt_eval_count": 11, "eval_count": 4, "message": {"content": ""}}]
        return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())

    b = OllamaBackend("http://o.test", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    data, usage = call(b)
    assert data == {"content": "hi"} and usage == {"input": 11, "output": 4, "cache_read": 0}


def test_ollama_missing_model_is_explained():
    b = OllamaBackend("http://o.test", http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"error": "model 'x' not found"}))))
    with pytest.raises(LLMError, match="isn't downloaded"):
        call(b)


def test_pick_gemini_model():
    assert providers.pick_gemini_model(["models/gemini-flash-latest", "models/gemini-2.5-pro"]) == "gemini-flash-latest"
    ids = ["models/gemini-2.0-flash", "models/gemini-2.5-flash", "models/gemini-2.5-flash-lite",
           "models/gemini-2.5-flash-image", "models/gemini-3.0-flash-preview", "models/text-embedding-004"]
    assert providers.pick_gemini_model(ids) == "gemini-3.0-flash-preview"
    assert providers.pick_gemini_model(["models/embedding-001"]) is None


def test_rank_openrouter_free_by_size_when_unknown():
    models = [{"id": i, "context_length": 200000} for i in (
        "google/gemma-4-26b-a4b-it:free", "nvidia/nemotron-3-ultra-550b-a55b:free", "liquid/lfm-3-1.7b:free",
        "cohere/north-mini-code:free", "x/plain-model:free")]
    assert [m["id"] for m in providers.rank_openrouter_free(models)] == [
        "nvidia/nemotron-3-ultra-550b-a55b:free", "x/plain-model:free", "google/gemma-4-26b-a4b-it:free"]


def test_rank_openrouter_free():
    models = [
        {"id": "meta-llama/llama-3.3-70b-instruct:free", "context_length": 131072},
        {"id": "qwen/qwen3-coder:free", "context_length": 262144},
        {"id": "tiny/model:free", "context_length": 8192},
        {"id": "anthropic/claude-x", "context_length": 200000},
    ]
    assert [m["id"] for m in providers.rank_openrouter_free(models)] == [
        "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen3-coder:free"]


def test_models_for_each_provider():
    s = Settings()
    assert providers.models_for(s) == ("claude-opus-5-5", "claude-haiku-5-5")
    s.provider = "ollama"
    assert providers.models_for(s) == ("qwen2.5-coder:14b", "qwen2.5-coder:14b")


def test_choices_file_then_config_precedence(tmp_path):
    cfg = tmp_path / "config.toml"
    save_choices({"provider": "ollama", "ollama_model": "qwen3-coder:30b"}, tmp_path / "choices.toml")
    s = load_settings(cfg)
    assert s.provider == "ollama" and s.ollama_model == "qwen3-coder:30b"
    cfg.write_text('provider = "gemini"\n')
    assert load_settings(cfg).provider == "gemini"


class ScriptedBackend:
    name = "ollama"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a, {"input": 1, "output": 1, "cache_read": 0}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "settings.gradle.kts").write_text('include(":app")\n')
    (tmp_path / "app").mkdir()
    (tmp_path / "app/build.gradle.kts").write_text('android { namespace = "com.ex" }\n')
    return tmp_path


def test_free_model_gets_second_chance(project):
    plan = {"summary": "s", "approach": "a", "files": [{"path": "app/A.kt", "purpose": "p", "spec": "x"}]}
    backend = ScriptedBackend([BadJSON("bad"), plan, {"notes": "forgot content"}, {"content": "class A"}])
    s = Settings(provider="ollama", review=False)
    sol, _ = run(Pipeline(s, backend=backend).solve(TaskInput("t", [], ["t"]), scan_project(project)))
    assert sol.files[0].content == "class A\n"
    assert len(backend.calls) == 4
    assert "not usable" in backend.calls[1]["content"][-1]["text"]
    assert all(c["model"] == "qwen2.5-coder:14b" for c in backend.calls)


def test_incomplete_xml_gets_retry(project):
    from autotyper.llm.pipeline import incomplete_reason

    assert incomplete_reason("a.xml", "<?xml version=")
    assert incomplete_reason("A.kt", "class A {") and not incomplete_reason("A.kt", "class A {}")
    plan = {"summary": "s", "files": [{"path": "app/src/main/res/values/strings.xml", "spec": "x"}]}
    backend = ScriptedBackend([plan, {"content": "<?xml version="}, {"content": "<resources>\n</resources>"}])
    sol, _ = run(Pipeline(Settings(provider="ollama", review=False), backend=backend).solve(
        TaskInput("t", [], ["t"]), scan_project(project)))
    assert sol.files[0].content == "<resources>\n</resources>\n"
    assert "cut off" in backend.calls[2]["content"][-1]["text"]
