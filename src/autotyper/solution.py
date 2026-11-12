"""Data model for a generated solution and its on-disk session folder."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class FilePlan:
    path: str  # project-relative, forward slashes
    action: str  # "create" | "modify"
    purpose: str
    spec: str  # detailed instructions for the worker
    order: int = 0


@dataclass
class Plan:
    summary: str
    approach: str
    files: list[FilePlan]
    user_steps: list[str] = field(default_factory=list)  # e.g. "Click Sync Now"
    explanation: str = ""  # teaching write-up for the user (markdown)


@dataclass
class FileResult:
    path: str
    action: str
    content: str
    original: str | None = None  # file content before the change (modify only)


@dataclass
class Solution:
    task: str
    project_root: str
    plan: Plan
    files: list[FileResult]
    created_at: float = field(default_factory=time.time)

    # ---- persistence -------------------------------------------------
    def save(self, session_dir: Path) -> Path:
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "solution.json").write_text(json.dumps(asdict(self), indent=2))
        (session_dir / "TASK.md").write_text(self.task)
        if self.plan.explanation:
            (session_dir / "EXPLANATION.md").write_text(self.plan.explanation)
        for f in self.files:
            out = session_dir / "files" / f.path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f.content)
        return session_dir

    @classmethod
    def load(cls, session_dir: Path) -> "Solution":
        raw = json.loads((session_dir / "solution.json").read_text())
        plan = raw["plan"]
        return cls(
            task=raw["task"],
            project_root=raw["project_root"],
            plan=Plan(
                summary=plan["summary"],
                approach=plan["approach"],
                files=[FilePlan(**fp) for fp in plan["files"]],
                user_steps=plan.get("user_steps", []),
                explanation=plan.get("explanation", ""),
            ),
            files=[FileResult(**f) for f in raw["files"]],
            created_at=raw.get("created_at", time.time()),
        )


def new_session_dir(project_root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return project_root / ".autotyper" / f"session-{stamp}"
