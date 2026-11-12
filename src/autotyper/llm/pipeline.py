"""Orchestrator/worker pipeline.

    task + project ──► planner (Opus 5.5 @ xhigh)   plan: which files, exact specs
                         │
            ┌────────────┼────────────┐
            ▼            ▼            ▼
         writer       writer       writer          one per file (Haiku 5.5), in parallel
            └────────────┼────────────┘
                         ▼
                 planner review                     cross-file consistency fixes

With a free provider (Ollama, Gemini, OpenRouter) one model plays every role;
see `autotyper.providers` and `autotyper.llm.backends`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
from typing import Callable

from ..config import Settings
from ..project_context import ProjectContext, render_context
from ..solution import FilePlan, FileResult, Plan, Solution
from ..task_input import TaskInput, to_content_blocks
from . import prompts
from .backends import BadJSON, ClaudeBackend, LLMError

__all__ = ["LLMError", "Pipeline", "safe_relpath"]

MAX_FILES = 20
_FORBIDDEN_DIRS = {"build", ".gradle", ".idea", ".git", ".autotyper"}


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
        client=None,
        on_status: Callable[[str], None] | None = None,
        backend=None,
    ):
        from .. import providers

        self.s = settings
        if backend is None:
            backend = ClaudeBackend(client) if client is not None else providers.make_backend(settings)
        self.backend = backend
        if hasattr(backend, "notice"):
            backend.notice = lambda msg: self.on_status(msg)
        self.planner_model, self.worker_model = providers.models_for(settings)
        self.parallel = settings.max_parallel_workers if settings.provider == "claude" else providers.get(settings.provider).parallel
        self.on_status = on_status or (lambda msg: None)
        self.usage: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ API
    async def _call(self, *, model, effort, system, content, schema, max_tokens, label, need=(), check=None) -> dict:
        """One model call that must return a JSON object containing the `need` keys.

        Weaker (free) models get a second chance when the answer is malformed.
        """
        tries = 1 if self.backend.name == "claude" else 2
        problem = ""
        for attempt in range(tries):
            ask = content if not problem else content + [{
                "type": "text",
                "text": f"Your previous answer was not usable ({problem}). Reply again with the complete JSON object only.",
            }]

            def progress(n: int) -> None:
                self.on_status(f"{label} with {model}... ({n} tokens so far)")

            try:
                data, usage = await self.backend.complete(
                    model=model, effort=effort, system=system, content=ask, schema=schema,
                    max_tokens=max_tokens, label=label, progress=progress,
                )
            except BadJSON:
                if attempt + 1 < tries:
                    problem = "it was not valid JSON"
                    continue
                raise
            u = self.usage.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
            for k in u:
                u[k] += usage.get(k, 0)
            missing = [k for k in need if not data.get(k)]
            if missing:
                problem = "missing " + ", ".join(missing)
                continue
            problem = check(data) if check and tries > 1 else ""
            if not problem:
                return data
        raise LLMError(f"The {label} answer was incomplete ({problem}). Try again, or use a stronger model.")

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
        self.on_status(f"Planning with {self.planner_model}...")
        content = self._shared_prefix(task, ctx) + [
            {"type": "text", "text": "Plan the complete solution for the task in this project."}
        ]
        raw = await self._call(
            model=self.planner_model, effort=self.s.orchestrator_effort,
            system=prompts.ORCHESTRATOR_SYSTEM, content=content, schema=prompts.PLAN_SCHEMA,
            max_tokens=64000, label="planning", need=("files",),
        )
        files = []
        seen = set()
        for f in sorted((f for f in raw.get("files") or [] if isinstance(f, dict) and f.get("path")),
                        key=lambda f: f.get("order", 0) if isinstance(f.get("order", 0), (int, float)) else 0):
            path = safe_relpath(ctx.root, f["path"])
            if path in seen:
                continue
            seen.add(path)
            files.append(FilePlan(path=path, action=f.get("action", "create"), purpose=f.get("purpose", ""),
                                  spec=f.get("spec", ""), order=f.get("order", len(files) + 1)))
        if not files:
            raise LLMError("The plan contains no files to write.")
        if len(files) > MAX_FILES:
            raise LLMError(f"The plan touches {len(files)} files (limit {MAX_FILES}). Split the task.")
        return Plan(summary=raw.get("summary", ""), approach=raw.get("approach", ""), files=files,
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
        sem = asyncio.Semaphore(max(1, self.parallel))
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
                    model=self.worker_model, effort=self.s.worker_effort, system=prompts.WORKER_SYSTEM,
                    content=prefix + [{"type": "text", "text": ask}], schema=prompts.WORKER_SCHEMA,
                    max_tokens=32000, label=f"writing {fp.path}", need=("content",),
                    check=lambda d, path=fp.path: incomplete_reason(path, str(d.get("content", ""))),
                )
            done += 1
            self.on_status(f"Writing files with {self.worker_model}: {done}/{len(plan.files)} done")
            return FileResult(path=fp.path, action=action, content=_strip_fences(raw["content"]), original=original)

        self.on_status(f"Writing files with {self.worker_model}: 0/{len(plan.files)} done")
        return list(await asyncio.gather(*(one(fp) for fp in plan.files)))

    async def review(self, task: TaskInput, ctx: ProjectContext, plan: Plan, files: list[FileResult]) -> tuple[list[FileResult], list[str]]:
        self.on_status(f"Reviewing with {self.planner_model}...")
        body = "\n".join(f'<file path="{f.path}">\n{f.content}\n</file>' for f in files)
        content = self._shared_prefix(task, ctx) + [
            {"type": "text", "text": self._plan_text(plan)},
            {"type": "text", "text": f"<team_output>\n{body}\n</team_output>\nReview the team's files."},
        ]
        raw = await self._call(
            model=self.planner_model, effort=self.s.orchestrator_effort, system=prompts.REVIEW_SYSTEM,
            content=content, schema=prompts.REVIEW_SCHEMA, max_tokens=64000, label="review",
        )
        by_path = {f.path: f for f in files}
        for fix in raw.get("files") or []:
            if not isinstance(fix, dict) or not fix.get("path") or not fix.get("content"):
                continue
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


def incomplete_reason(path: str, code: str) -> str:
    """Cheap sanity check for cut-off files from weaker models ('' when it looks complete)."""
    body = _strip_fences(code).strip()
    if path.endswith(".xml"):
        if not body.endswith(">") or body.count("<") < 2:
            return "the XML file is cut off - write the whole file"
    elif path.endswith((".kt", ".kts", ".java")):
        if body.count("{") != body.count("}"):
            return "the braces { } don't balance - write the whole file"
    return ""


def _strip_fences(code: str) -> str:
    s = code.strip("\n")
    if s.startswith("```"):
        lines = s.split("\n")
        if lines[-1].strip().startswith("```"):
            s = "\n".join(lines[1:-1])
    return s.rstrip() + "\n"
