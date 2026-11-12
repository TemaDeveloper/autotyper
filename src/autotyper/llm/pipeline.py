"""Orchestrator/worker pipeline.

    task + project ──► Opus 5.5 (xhigh)   plan: which files, exact specs
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
       Haiku 5.5    Haiku 5.5    Haiku 5.5      one worker per file, in parallel
            └────────────┼────────────┘
                         ▼
                 Opus 5.5 review                cross-file consistency fixes
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import anthropic

from ..config import Settings
from ..credentials import client_kwargs
from ..project_context import ProjectContext, render_context
from ..solution import FilePlan, FileResult, Plan, Solution
from ..task_input import TaskInput, to_content_blocks
from . import prompts

MAX_FILES = 20
_FORBIDDEN_DIRS = {"build", ".gradle", ".idea", ".git", ".autotyper"}


class LLMError(Exception):
    pass


def safe_relpath(root: Path, rel: str) -> str:
    """Validate a model-proposed path: relative, inside the project, not in build/IDE dirs."""
    rel = rel.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts:
        raise LLMError(f"Refusing unsafe path from the model: {rel!r}")
    if _FORBIDDEN_DIRS & set(p.parts) or p.name in {"local.properties"}:
        raise LLMError(f"Refusing to write into a build/IDE path: {rel!r}")
    full = (root / rel).resolve()
    if not full.is_relative_to(root.resolve()):
        raise LLMError(f"Path escapes the project: {rel!r}")
    return str(p)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        client: anthropic.AsyncAnthropic | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self.s = settings
        self.client = client or anthropic.AsyncAnthropic(**client_kwargs())
        self.on_status = on_status or (lambda msg: None)
        self.usage: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ API
    async def _call(
        self,
        *,
        model: str,
        effort: str,
        system: str,
        content: list[dict],
        schema: dict,
        max_tokens: int,
        label: str,
    ) -> dict:
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
            raise LLMError("Anthropic API key missing or invalid. Run `autotyper setup` to add your key.") from e
        except anthropic.RateLimitError as e:
            raise LLMError(f"Rate limited by the Anthropic API ({label}). Try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error during {label}: {e.status_code} {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Network error during {label}: {e}") from e

        u = self.usage.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
        u["input"] += getattr(msg.usage, "input_tokens", 0) or 0
        u["output"] += getattr(msg.usage, "output_tokens", 0) or 0
        u["cache_read"] += getattr(msg.usage, "cache_read_input_tokens", 0) or 0

        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise LLMError(f"The model declined the {label} step (category: {cat}).")
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"The {label} output was cut off (max_tokens). Try a smaller task.")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Could not parse the {label} response as JSON.") from e

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _shared_prefix(task: TaskInput, ctx: ProjectContext) -> list[dict]:
        blocks: list[dict] = [
            {"type": "text", "text": "<project_context>\n" + render_context(ctx) + "\n</project_context>",
             "cache_control": {"type": "ephemeral"}},
        ]
        task_blocks = to_content_blocks(task)
        task_blocks[-1] = {"type": "text", "text": "<task>\n" + task_blocks[-1]["text"] + "\n</task>"}
        blocks.extend(task_blocks)
        return blocks

    async def plan(self, task: TaskInput, ctx: ProjectContext) -> Plan:
        self.on_status(f"Planning with {self.s.orchestrator_model} (effort {self.s.orchestrator_effort})...")
        content = self._shared_prefix(task, ctx) + [
            {"type": "text", "text": "Plan the complete solution for the task in this project."}
        ]
        raw = await self._call(
            model=self.s.orchestrator_model, effort=self.s.orchestrator_effort,
            system=prompts.ORCHESTRATOR_SYSTEM, content=content, schema=prompts.PLAN_SCHEMA,
            max_tokens=64000, label="planning",
        )
        files = []
        seen = set()
        for f in sorted(raw["files"], key=lambda f: f.get("order", 0)):
            path = safe_relpath(ctx.root, f["path"])
            if path in seen:
                continue
            seen.add(path)
            files.append(FilePlan(path=path, action=f["action"], purpose=f["purpose"], spec=f["spec"], order=f["order"]))
        if not files:
            raise LLMError("The plan contains no files to write.")
        if len(files) > MAX_FILES:
            raise LLMError(f"The plan touches {len(files)} files (limit {MAX_FILES}). Split the task.")
        return Plan(summary=raw["summary"], approach=raw["approach"], files=files,
                    user_steps=raw.get("user_steps", []), explanation=raw.get("explanation", ""))

    @staticmethod
    def _plan_text(plan: Plan) -> str:
        lines = [f"Summary: {plan.summary}", f"Approach: {plan.approach}", "", "Files in this change:"]
        for f in plan.files:
            lines += [f"--- {f.path} ({f.action}) ---", f"Purpose: {f.purpose}", f"Spec:\n{f.spec}", ""]
        return "<plan>\n" + "\n".join(lines) + "\n</plan>"

    async def write_files(self, task: TaskInput, ctx: ProjectContext, plan: Plan) -> list[FileResult]:
        prefix = self._shared_prefix(task, ctx) + [
            {"type": "text", "text": self._plan_text(plan), "cache_control": {"type": "ephemeral"}}
        ]
        sem = asyncio.Semaphore(max(1, self.s.max_parallel_workers))
        done = 0

        async def one(fp: FilePlan) -> FileResult:
            nonlocal done
            full = ctx.root / fp.path
            original = full.read_text(encoding="utf-8", errors="replace") if full.exists() else None
            action = "modify" if original is not None else "create"
            ask = f"Write the file `{fp.path}` ({action}).\nPurpose: {fp.purpose}\nSpec:\n{fp.spec}\n"
            if original is not None:
                ask += f"\nCurrent content of {fp.path}:\n<current_file>\n{original}\n</current_file>\n"
            async with sem:
                raw = await self._call(
                    model=self.s.worker_model, effort=self.s.worker_effort, system=prompts.WORKER_SYSTEM,
                    content=prefix + [{"type": "text", "text": ask}], schema=prompts.WORKER_SCHEMA,
                    max_tokens=32000, label=f"writing {fp.path}",
                )
            done += 1
            self.on_status(f"Workers ({self.s.worker_model}): {done}/{len(plan.files)} files written")
            return FileResult(path=fp.path, action=action, content=_strip_fences(raw["content"]), original=original)

        self.on_status(f"Workers ({self.s.worker_model}): 0/{len(plan.files)} files written")
        return list(await asyncio.gather(*(one(fp) for fp in plan.files)))

    async def review(self, task: TaskInput, ctx: ProjectContext, plan: Plan, files: list[FileResult]) -> tuple[list[FileResult], list[str]]:
        self.on_status(f"Reviewing with {self.s.orchestrator_model}...")
        body = "\n".join(f'<file path="{f.path}">\n{f.content}\n</file>' for f in files)
        content = self._shared_prefix(task, ctx) + [
            {"type": "text", "text": self._plan_text(plan)},
            {"type": "text", "text": f"<team_output>\n{body}\n</team_output>\nReview the team's files."},
        ]
        raw = await self._call(
            model=self.s.orchestrator_model, effort=self.s.orchestrator_effort, system=prompts.REVIEW_SYSTEM,
            content=content, schema=prompts.REVIEW_SCHEMA, max_tokens=64000, label="review",
        )
        by_path = {f.path: f for f in files}
        for fix in raw.get("files", []):
            path = safe_relpath(ctx.root, fix["path"])
            if path in by_path:
                by_path[path].content = _strip_fences(fix["content"])
        return [by_path[f.path] for f in files], raw.get("issues", [])

    async def solve(self, task: TaskInput, ctx: ProjectContext) -> tuple[Solution, list[str]]:
        plan = await self.plan(task, ctx)
        files = await self.write_files(task, ctx, plan)
        issues: list[str] = []
        if self.s.review:
            files, issues = await self.review(task, ctx, plan, files)
        return Solution(task=task.text, project_root=str(ctx.root), plan=plan, files=files), issues


def _strip_fences(code: str) -> str:
    s = code.strip("\n")
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[-1].strip().startswith("```"):
            s = "\n".join(lines[1:-1])
    return s.rstrip() + "\n"
