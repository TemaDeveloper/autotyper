"""Scan an Android (Gradle) project and render a compact, budgeted text context for an LLM."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

TREE_MAX_LINES = 400

PRUNE_DIRS = {
    "build",
    ".gradle",
    ".idea",
    ".git",
    ".autotyper",
    ".cxx",
    ".kotlin",
    "node_modules",
    "captures",
    "intermediates",
    "generated",
}
RELEVANT_EXTS = {".kt", ".kts", ".java", ".xml", ".gradle", ".toml", ".properties", ".pro"}
EXCLUDED_NAMES = {"local.properties", "google-services.json"}
# Keystores (*.jks, *.keystore) are never in RELEVANT_EXTS, so they are excluded implicitly.

NAMESPACE_RE = re.compile(r"""^\s*namespace\s*(?:=\s*|\s+)["']([^"']+)["']""", re.MULTILINE)
MANIFEST_PACKAGE_RE = re.compile(r"""<manifest\b[^>]*?\bpackage\s*=\s*["']([^"']+)["']""")


class ProjectError(Exception):
    """Raised when a project root cannot be found or scanned."""


@dataclass
class ProjectContext:
    root: Path
    tree: str
    files: dict[str, str]
    omitted: list[str]
    language: str
    uses_compose: bool
    namespace: str | None


def find_project_root(start: Path) -> Path:
    """Walk up from ``start`` (file or directory) to the nearest Gradle settings directory."""
    path = Path(start).resolve()
    if not path.is_dir():
        path = path.parent
    for candidate in (path, *path.parents):
        if (candidate / "settings.gradle").is_file() or (candidate / "settings.gradle.kts").is_file():
            return candidate
    raise ProjectError(f"No settings.gradle or settings.gradle.kts found at or above {start}")


def is_text_file(path: Path) -> bool:
    return _read_text(path) is not None


def _read_text(path: Path) -> str | None:
    """Return the decoded UTF-8 content, or None if the file is binary or undecodable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _prune_dir(name: str, parent_name: str) -> bool:
    if name in PRUNE_DIRS or name.startswith("."):
        return True
    if parent_name == "res":
        if name.startswith("mipmap"):
            return True
        if name.startswith("drawable-") and name.endswith("dpi"):
            return True
        if name in ("font", "raw"):
            return True
    return False


def _is_relevant(rel: str) -> bool:
    parts = rel.split("/")
    name = parts[-1]
    if name in EXCLUDED_NAMES:
        return False
    suffix = os.path.splitext(name)[1]
    if suffix == ".json":
        return "src" in parts[:-1]
    return suffix in RELEVANT_EXTS


def _has_seq(parts: list[str], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    return any(tuple(parts[i : i + n]) == seq for i in range(len(parts) - n + 1))


def _walk_relevant(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)
        parent_name = dir_path.name
        dirnames[:] = sorted(d for d in dirnames if not _prune_dir(d, parent_name))
        for filename in filenames:
            rel = (dir_path / filename).relative_to(root).as_posix()
            if _is_relevant(rel):
                found.append(rel)
    return found


def _priority(rel: str) -> int:
    parts = rel.split("/")
    name = parts[-1]
    depth = len(parts)
    if depth == 1 and name in ("settings.gradle", "settings.gradle.kts"):
        return 0
    if rel == "gradle/libs.versions.toml":
        return 1
    if depth == 1 and name in ("build.gradle", "build.gradle.kts", "gradle.properties"):
        return 2
    if name in ("build.gradle", "build.gradle.kts"):
        return 3
    if name == "AndroidManifest.xml":
        return 4
    if _has_seq(parts[:-1], ("src", "main", "java")) or _has_seq(parts[:-1], ("src", "main", "kotlin")):
        return 5
    if "res" in parts[:-1]:
        idx = parts.index("res")
        sub = parts[idx + 1] if idx + 1 < depth - 1 else ""
        if sub.startswith("layout") or sub in ("navigation", "menu"):
            return 6
        if sub.startswith("values"):
            return 7
        return 8
    if "test" in parts[:-1] or "androidTest" in parts[:-1]:
        return 9
    return 10


def _build_tree(rels: list[str]) -> str:
    root_node: dict = {}
    for rel in rels:
        parts = rel.split("/")
        node = root_node
        for directory in parts[:-1]:
            node = node.setdefault(directory, {})
        node.setdefault(parts[-1], None)

    lines: list[str] = []

    def walk(node: dict, depth: int) -> None:
        for name in sorted(node, key=lambda n: (node[n] is None, n)):
            child = node[name]
            indent = "  " * depth
            if child is None:
                lines.append(indent + name)
            else:
                lines.append(indent + name + "/")
                walk(child, depth + 1)

    walk(root_node, 0)
    if len(lines) > TREE_MAX_LINES:
        extra = len(lines) - TREE_MAX_LINES
        lines = lines[:TREE_MAX_LINES] + [f"... ({extra} more)"]
    return "\n".join(lines)


def _detect_language(rels: list[str]) -> str:
    kotlin = java = 0
    for rel in rels:
        if not _has_seq(rel.split("/")[:-1], ("src", "main")):
            continue
        if rel.endswith(".kt"):
            kotlin += 1
        elif rel.endswith(".java"):
            java += 1
    if kotlin and java:
        return "mixed"
    if kotlin:
        return "kotlin"
    if java:
        return "java"
    return "unknown"


def _detect_namespace(rels: list[str], load) -> str | None:
    build_files = [r for r in rels if r.rsplit("/", 1)[-1] in ("build.gradle", "build.gradle.kts")]
    build_files.sort(key=lambda r: (r not in ("app/build.gradle", "app/build.gradle.kts"), r))
    for rel in build_files:
        match = NAMESPACE_RE.search(load(rel) or "")
        if match:
            return match.group(1)

    manifests = [r for r in rels if r.rsplit("/", 1)[-1] == "AndroidManifest.xml"]
    manifests.sort(key=lambda r: (r != "app/src/main/AndroidManifest.xml", r))
    for rel in manifests:
        match = MANIFEST_PACKAGE_RE.search(load(rel) or "")
        if match:
            return match.group(1)
    return None


def scan_project(root: Path, budget_chars: int = 250_000, max_file_chars: int = 60_000) -> ProjectContext:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ProjectError(f"Project root is not a directory: {root}")

    rels = sorted(_walk_relevant(root), key=lambda r: (_priority(r), r))

    cache: dict[str, str | None] = {}

    def load(rel: str) -> str | None:
        if rel not in cache:
            cache[rel] = _read_text(root / rel)
        return cache[rel]

    files: dict[str, str] = {}
    omitted: list[str] = []
    used = 0
    exhausted = False
    for rel in rels:
        text = load(rel)
        if text is None:
            continue  # non-text: skipped silently
        if exhausted or len(text) > max_file_chars:
            omitted.append(rel)
            continue
        if used + len(text) > budget_chars:
            exhausted = True
            omitted.append(rel)
            continue
        files[rel] = text
        used += len(text)

    uses_compose = False
    for rel in rels:
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("build.gradle") or name == "libs.versions.toml":
            if "compose" in (load(rel) or "").lower():
                uses_compose = True
                break

    return ProjectContext(
        root=root,
        tree=_build_tree(rels),
        files=files,
        omitted=omitted,
        language=_detect_language(rels),
        uses_compose=uses_compose,
        namespace=_detect_namespace(rels, load),
    )


def render_context(ctx: ProjectContext) -> str:
    namespace = ctx.namespace or ""
    out = [
        f'<project root="{ctx.root.name}" language="{ctx.language}" '
        f'compose="{str(ctx.uses_compose).lower()}" namespace="{namespace}">\n',
        "<tree>\n",
        ctx.tree + "\n",
        "</tree>\n",
    ]
    for path, content in ctx.files.items():
        body = content if (not content or content.endswith("\n")) else content + "\n"
        out.append(f'<file path="{path}">\n')
        out.append(body)
        out.append("</file>\n")
    if ctx.omitted:
        out.append("<omitted>" + "\n".join(ctx.omitted) + "</omitted>\n")
    out.append("</project>\n")
    return "".join(out)
