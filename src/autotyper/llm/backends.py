"""Model backends: one `complete()` call shape for every AI provider.

The pipeline builds Anthropic-style content blocks (text / image / document)
and asks for a JSON object matching a schema. Each backend turns that into
its provider's request format and returns (parsed_json, usage).

  ClaudeBackend        Anthropic Messages API (default; Opus plans, Haiku writes)
  OllamaBackend        local models via Ollama's native /api/chat (free, offline)
  OpenAICompatBackend  OpenAI-style /chat/completions: Google Gemini, OpenRouter
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, Callable

import httpx

ProgressFn = Callable[[int], None]


class LLMError(Exception):
    pass


class BadJSON(LLMError):
    """The model answered, but not with usable JSON. Worth one retry on weaker models."""


# ---------------------------------------------------------------------- helpers
def extract_json(text: str) -> dict:
    """Parse a JSON object from model text, tolerating code fences and chatter around it."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```\s*$", "", s)
    try:
        val = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise
        val = json.loads(s[start : end + 1])
    if not isinstance(val, dict):
        raise json.JSONDecodeError("not an object", s, 0)
    return val


def missing_required(data: dict, schema: dict) -> list[str]:
    return [k for k in schema.get("required", []) if k not in data]


def pdf_to_text(data_b64: str) -> str:
    """Text of a PDF via macOS PDFKit (for providers that can't read PDFs)."""
    try:
        import Quartz
        from Foundation import NSData
    except ImportError as e:
        raise LLMError("This AI can't read PDFs on this system. Copy the task text instead.") from e
    raw = base64.b64decode(data_b64)
    doc = Quartz.PDFDocument.alloc().initWithData_(NSData.dataWithBytes_length_(raw, len(raw)))
    text = str(doc.string() or "") if doc else ""
    if not text.strip():
        raise LLMError("This PDF has no selectable text (it may be scanned). Take a screenshot of it instead.")
    return text


def split_content(content: list[dict]) -> tuple[str, list[tuple[str, str]]]:
    """Anthropic content blocks -> (all text, [(media_type, base64 image)]). PDFs become text."""
    texts: list[str] = []
    images: list[tuple[str, str]] = []
    for b in content:
        t = b.get("type")
        if t == "text":
            texts.append(b["text"])
        elif t == "image":
            images.append((b["source"]["media_type"], b["source"]["data"]))
        elif t == "document":
            texts.append("<task_document>\n" + pdf_to_text(b["source"]["data"]) + "\n</task_document>")
    return "\n\n".join(texts), images


def schema_instruction(schema: dict) -> str:
    return (
        "\n\nOUTPUT FORMAT: reply with ONE JSON object and nothing else (no prose, no code fences). "
        "It must match this JSON Schema:\n" + json.dumps(schema)
    )


# ---------------------------------------------------------------------- Claude
class ClaudeBackend:
    name = "claude"

    def __init__(self, client=None):
        import anthropic

        from ..credentials import client_kwargs

        self.anthropic = anthropic
        self.client = client or anthropic.AsyncAnthropic(**client_kwargs())

    async def complete(self, *, model, effort, system, content, schema, max_tokens, label, progress=None):
        anthropic = self.anthropic
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")):
            # Server-side refusal fallback, routed by refusal category.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            async with self.client.beta.messages.stream(**kwargs) as stream:
                msg = await stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Not connected to Claude. Run `autotyper login`.") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Rate limited by the Anthropic API ({label}). Try again shortly.") from e
        except anthropic.APIStatusError as e:
            text = str(getattr(e, "message", "")) + str(getattr(e, "body", ""))
            if "credit balance is too low" in text:
                raise LLMError(
                    "Your Claude Console account has no API credits. Add credits at "
                    "https://platform.claude.com/settings/billing (a claude.ai subscription doesn't cover the API), "
                    "or use a free AI: `autotyper login` → Ollama / Gemini / OpenRouter."
                ) from e
            raise LLMError(f"Anthropic API error during {label}: {e.status_code} {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Network error during {label}: {e}") from e

        usage = {
            "input": getattr(msg.usage, "input_tokens", 0) or 0,
            "output": getattr(msg.usage, "output_tokens", 0) or 0,
            "cache_read": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        }
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise LLMError(f"The model declined the {label} step (category: {cat}).")
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"The {label} output was cut off (max_tokens). Try a smaller task.")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as e:
            raise LLMError(f"Could not parse the {label} response as JSON.") from e


# ---------------------------------------------------------------------- Ollama
class OllamaBackend:
    """Ollama's native API: lets us set the context window and enforce the JSON schema."""

    name = "ollama"
    num_ctx = 32768

    def __init__(self, base_url: str = "http://localhost:11434", http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=10.0))

    async def complete(self, *, model, effort, system, content, schema, max_tokens, label, progress=None):
        text, images = split_content(content)
        user: dict[str, Any] = {"role": "user", "content": text}
        if images:
            user["images"] = [data for _, data in images]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system + schema_instruction(schema)}, user],
            "format": schema,
            "stream": True,
            "options": {"num_ctx": self.num_ctx, "temperature": 0.2, "num_predict": min(max_tokens, 16384)},
        }
        parts: list[str] = []
        final: dict = {}
        try:
            async with self.http.stream("POST", f"{self.base_url}/api/chat", json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode(errors="replace")[:300]
                    if r.status_code == 404 or "not found" in detail:
                        raise LLMError(f"Ollama model '{model}' isn't downloaded. Run `autotyper login` → Ollama.")
                    raise LLMError(f"Ollama error during {label}: {r.status_code} {detail}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise LLMError(f"Ollama error during {label}: {chunk['error']}")
                    parts.append(chunk.get("message", {}).get("content", ""))
                    if progress and len(parts) % 25 == 0:
                        progress(len(parts))
                    if chunk.get("done"):
                        final = chunk
        except httpx.ConnectError as e:
            raise LLMError("Ollama isn't running. Open the Ollama app (or run `ollama serve`) and try again.") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama connection problem during {label}: {e}") from e
        if final.get("done_reason") == "length":
            raise LLMError(f"The {label} output was cut off. Try a smaller task or a bigger model.")
        usage = {"input": final.get("prompt_eval_count", 0) or 0, "output": final.get("eval_count", 0) or 0, "cache_read": 0}
        try:
            return extract_json("".join(parts)), usage
        except json.JSONDecodeError as e:
            raise BadJSON(f"The model's {label} answer wasn't valid JSON.") from e


# ---------------------------------------------------------------------- OpenAI-compatible
class OpenAICompatBackend:
    """Gemini (generativelanguage.googleapis.com/v1beta/openai) and OpenRouter (openrouter.ai/api/v1)."""

    FORMATS = ("json_schema", "json_object", "none")

    def __init__(self, name: str, base_url: str, api_key: str, http: httpx.AsyncClient | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep, max_retries: int = 4):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0))
        self.sleep = sleep
        self.max_retries = max_retries
        self.format_level = 0  # downgraded when a provider/model rejects structured output

    def _response_format(self, schema: dict) -> dict | None:
        level = self.FORMATS[self.format_level]
        if level == "json_schema":
            return {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        if level == "json_object":
            return {"type": "json_object"}
        return None

    async def complete(self, *, model, effort, system, content, schema, max_tokens, label, progress=None):
        text, images = split_content(content)
        user_content: Any = text
        if images:
            user_content = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}} for mt, data in images
            ]
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.name == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/TemaDeveloper/autotyper", "X-Title": "autotyper"})
        attempt = 0
        while True:
            body: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system + schema_instruction(schema)},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            rf = self._response_format(schema)
            if rf:
                body["response_format"] = rf
            if self.name == "gemini":
                body["reasoning_effort"] = {"low": "low", "medium": "medium"}.get(effort, "high")
            try:
                r = await self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise LLMError(f"Network error talking to {self.name} during {label}: {e}") from e

            if r.status_code == 429 or r.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    if r.status_code == 429:
                        raise LLMError(
                            f"{self.name} free-tier limit reached during {label}. Wait a minute (or until tomorrow "
                            "for daily limits) and run `autotyper solve` again, or switch AI with `autotyper login`."
                        )
                    raise LLMError(f"{self.name} server error during {label}: {r.status_code}")
                wait = _retry_after(r) or min(60.0, 5.0 * 2 ** (attempt - 1))
                await self.sleep(wait)
                continue
            if r.status_code == 400 and self.format_level < len(self.FORMATS) - 1:
                self.format_level += 1  # model can't do this structured-output mode; try a simpler one
                continue
            if r.status_code in (401, 403):
                raise LLMError(f"{self.name} rejected the API key. Run `autotyper login` to set it again.")
            if r.status_code == 404:
                raise LLMError(f"{self.name} doesn't have the model '{model}'. Run `autotyper login` to pick another.")
            if r.status_code != 200:
                raise LLMError(f"{self.name} error during {label}: {r.status_code} {r.text[:300]}")

            data = r.json()
            if data.get("error"):
                raise LLMError(f"{self.name} error during {label}: {data['error']}")
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise LLMError(f"The {label} output was cut off. Try a smaller task.")
            msg = choice.get("message") or {}
            text_out = msg.get("content") or ""
            if isinstance(text_out, list):
                text_out = "".join(p.get("text", "") for p in text_out if isinstance(p, dict))
            u = data.get("usage") or {}
            usage = {"input": u.get("prompt_tokens", 0) or 0, "output": u.get("completion_tokens", 0) or 0, "cache_read": 0}
            try:
                return extract_json(text_out), usage
            except json.JSONDecodeError as e:
                raise BadJSON(f"The model's {label} answer wasn't valid JSON.") from e


def _retry_after(r: httpx.Response) -> float | None:
    try:
        v = float(r.headers.get("retry-after", ""))
        return min(v, 90.0)
    except ValueError:
        pass
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', r.text)
    return min(float(m.group(1)), 90.0) if m else None
