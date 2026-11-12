"""AI providers autotyper can use, and helpers to set them up.

  claude      Anthropic API. Best results. Paid from Claude Console API credits.
  ollama      Local open models on this Mac. Free, offline, private.
  gemini      Google Gemini API free tier. Free key from Google AI Studio.
  openrouter  Free models on OpenRouter. Free key; the list of free models changes.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .credentials import credential_source, provider_key


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    blurb: str
    budget_chars: int  # how much project source to send
    parallel: int  # files written at once
    base_url: str = ""
    key_url: str = ""


PROVIDERS: dict[str, Provider] = {
    "claude": Provider("claude", "Claude (Anthropic)", "best results · paid from Claude Console API credits", 250_000, 4),
    "ollama": Provider("ollama", "Ollama (on this Mac)", "free · works offline · code never leaves your Mac", 60_000, 1),
    "gemini": Provider(
        "gemini", "Google Gemini", "free tier with daily limits · free key from Google", 250_000, 2,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_url="https://aistudio.google.com/apikey",
    ),
    "openrouter": Provider(
        "openrouter", "OpenRouter free models", "free models with rate limits · free key", 120_000, 2,
        base_url="https://openrouter.ai/api/v1",
        key_url="https://openrouter.ai/settings/keys",
    ),
}

OLLAMA_CHOICES = [
    # (model tag, size, description)
    ("qwen2.5-coder:7b", "4.7 GB", "fast, fine for small single-screen apps"),
    ("qwen2.5-coder:14b", "9 GB", "recommended: good balance of speed and quality"),
    ("qwen3-coder:30b", "19 GB", "best quality, needs 32 GB RAM, slower to load"),
]


def get(name: str) -> Provider:
    if name not in PROVIDERS:
        raise ValueError(f"Unknown AI provider '{name}'. Choose one of: {', '.join(PROVIDERS)}")
    return PROVIDERS[name]


def models_for(s: Settings) -> tuple[str, str]:
    """(planner model, writer model) for the chosen provider."""
    if s.provider == "claude":
        return s.orchestrator_model, s.worker_model
    model = {"ollama": s.ollama_model, "gemini": s.gemini_model, "openrouter": s.openrouter_model}[s.provider]
    return model, model


def make_backend(s: Settings):
    from .llm.backends import ClaudeBackend, LLMError, OllamaBackend, OpenAICompatBackend

    p = get(s.provider)
    if p.key == "claude":
        return ClaudeBackend()
    if p.key == "ollama":
        return OllamaBackend(s.ollama_url)
    key = provider_key(p.key)
    if not key:
        raise LLMError(f"No {p.label} key yet. Run `autotyper login` and choose {p.label}.")
    fallbacks: list[str] = []
    if p.key == "gemini":
        try:
            fallbacks = [m for m in gemini_candidates(list_gemini_models(key)) if m != s.gemini_model][:4]
        except (httpx.HTTPError, ValueError):
            fallbacks = []
    return OpenAICompatBackend(p.key, p.base_url, key, fallback_models=fallbacks)


def readiness(s: Settings) -> tuple[bool, str]:
    """Is the chosen provider usable right now? (ok, human-readable detail)"""
    p = get(s.provider)
    if p.key == "claude":
        src = credential_source()
        return (True, src) if src else (False, "not connected - run autotyper login")
    if p.key == "ollama":
        if not ollama_running(s.ollama_url):
            return False, "Ollama isn't running - open the Ollama app"
        if not ollama_has_model(s.ollama_url, s.ollama_model):
            return False, f"model {s.ollama_model} not downloaded - run autotyper login"
        return True, s.ollama_model
    if not provider_key(p.key):
        return False, "no API key - run autotyper login"
    model = models_for(s)[0]
    if not model:
        return False, "no model picked - run autotyper login"
    return True, model


# ---------------------------------------------------------------------- Ollama
def ollama_installed() -> bool:
    return bool(shutil.which("ollama")) or Path("/Applications/Ollama.app").exists()


def ollama_running(url: str = "http://localhost:11434") -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}/api/tags", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def ollama_models(url: str = "http://localhost:11434") -> list[str]:
    try:
        data = httpx.get(f"{url.rstrip('/')}/api/tags", timeout=5.0).json()
    except (httpx.HTTPError, ValueError):
        return []
    return [m.get("name", "") for m in data.get("models", [])]


def ollama_has_model(url: str, model: str) -> bool:
    names = ollama_models(url)
    want = model if ":" in model else model + ":latest"
    return want in names or model in names


def start_ollama(url: str = "http://localhost:11434", wait: float = 15.0) -> bool:
    """Start the Ollama app or server in the background and wait until it answers."""
    if ollama_running(url):
        return True
    if Path("/Applications/Ollama.app").exists():
        subprocess.run(["open", "-g", "-a", "Ollama"], check=False, capture_output=True)
    elif shutil.which("ollama"):
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if ollama_running(url):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------- model discovery
_GEMINI_SKIP = ("lite", "image", "tts", "audio", "live", "embedding", "vision", "native", "learnlm", "gemma", "aqa")


def gemini_candidates(ids: list[str]) -> list[str]:
    """General Flash models (the free tier's workhorse), newest first; moving aliases last."""
    names = [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids]
    flash = [n for n in names if n.startswith("gemini-") and "flash" in n and not any(x in n for x in _GEMINI_SKIP)]

    def rank(n: str) -> tuple:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        version = float(m.group(1)) if m else 0.0
        stable = not any(x in n for x in ("preview", "exp"))
        return (version, stable, -len(n))

    versioned = sorted((n for n in flash if not n.endswith("latest")), key=rank, reverse=True)
    return versioned + [n for n in flash if n.endswith("latest")]


def pick_gemini_model(ids: list[str]) -> str | None:
    c = gemini_candidates(ids)
    return c[0] if c else None


def probe_model(provider: str, key: str, model: str, timeout: float = 20.0) -> str:
    """Send a tiny request. Returns '' if the model answers, else a short reason."""
    p = get(provider)
    try:
        r = httpx.post(
            f"{p.base_url}/chat/completions", headers={"Authorization": f"Bearer {key}"}, timeout=timeout,
            json={"model": model, "messages": [{"role": "user", "content": "Reply with: ok"}], "max_tokens": 300},
        )
    except httpx.TimeoutException:
        return "not responding"
    except httpx.HTTPError as e:
        return f"network error ({e.__class__.__name__})"
    if r.status_code == 200:
        return ""
    if r.status_code == 429:
        return "no free quota right now"
    if r.status_code == 404:
        return "not available"
    if r.status_code in (401, 403):
        return "key rejected"
    return f"error {r.status_code}"


def list_gemini_models(key: str) -> list[str]:
    r = httpx.get(f"{PROVIDERS['gemini'].base_url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15.0)
    r.raise_for_status()
    return [m.get("id", "") for m in r.json().get("data", [])]


_OR_PREFERENCE = ("qwen3-coder", "deepseek", "qwen", "kimi", "gpt-oss", "llama-3.3-70b", "glm", "mistral", "gemma")
_SMALL_WORDS = ("mini", "nano", "small", "lightning", "tiny", "lite")


def _param_billions(model_id: str) -> float:
    """Rough model size from its id ('...-550b-a55b' -> 550). Unknown -> 30; 'mini'/'nano' -> 5."""
    mid = model_id.lower()
    sizes = [float(x) for x in re.findall(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z])", mid)]
    if sizes:
        return max(sizes)
    return 5.0 if any(w in mid for w in _SMALL_WORDS) else 30.0


def rank_openrouter_free(models: list[dict]) -> list[dict]:
    """Free OpenRouter models likely to handle a multi-file coding task: biggest first, known coders break ties."""
    free = [
        m for m in models
        if str(m.get("id", "")).endswith(":free")
        and (m.get("context_length") or 0) >= 32_000
        and _param_billions(m["id"]) >= 7
    ]

    def score(m: dict) -> tuple:
        mid = m["id"].lower()
        pref = next((i for i, k in enumerate(_OR_PREFERENCE) if k in mid), len(_OR_PREFERENCE))
        return (-_param_billions(mid), pref, -(m.get("context_length") or 0))

    return sorted(free, key=score)


def list_openrouter_models() -> list[dict]:
    r = httpx.get(f"{PROVIDERS['openrouter'].base_url}/models", timeout=20.0)
    r.raise_for_status()
    return r.json().get("data", [])
