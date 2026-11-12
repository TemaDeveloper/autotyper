from pathlib import Path

import pytest

from autotyper.project_context import (
    ProjectError,
    find_project_root,
    is_text_file,
    render_context,
    scan_project,
)

SETTINGS = 'rootProject.name = "demo"\ninclude(":app")\n'
LIBS_COMPOSE = '[libraries]\ncompose-ui = "androidx.compose.ui:ui:1.6.0"\n'
LIBS_PLAIN = '[libraries]\nappcompat = "androidx.appcompat:appcompat:1.7.0"\n'
MANIFEST = '<manifest xmlns:android="http://schemas.android.com/apk/res/android">\n    <application />\n</manifest>\n'


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def make_project(base: Path, *, namespace: bool = True, compose: bool = True) -> Path:
    root = base / "demo"
    write(root / "settings.gradle.kts", SETTINGS)
    write(root / "gradle" / "libs.versions.toml", LIBS_COMPOSE if compose else LIBS_PLAIN)
    ns_line = '    namespace = "com.example.demo"\n' if namespace else ""
    write(
        root / "app" / "build.gradle.kts",
        'plugins {\n    id("com.android.application")\n}\nandroid {\n' + ns_line + "}\n",
    )
    write(root / "app" / "src" / "main" / "AndroidManifest.xml", MANIFEST)
    write(
        root / "app" / "src" / "main" / "java" / "com" / "example" / "demo" / "MainActivity.kt",
        "package com.example.demo\n\nclass MainActivity\n",
    )
    write(root / "app" / "src" / "main" / "res" / "layout" / "activity_main.xml", "<LinearLayout/>\n")
    write(root / "app" / "src" / "main" / "res" / "values" / "strings.xml", "<resources/>\n")
    # Pruned or excluded material
    write(root / "app" / "build" / "generated" / "X.kt", "// generated\n")
    write(root / "local.properties", "sdk.dir=/Users/me/Library/Android/sdk\n")
    write(root / "app" / "src" / "main" / "res" / "drawable-hdpi" / "icon.png", b"\x89PNG\r\n\x1a\n\x00\x00")
    write(root / ".idea" / "x.xml", "<x/>\n")
    write(root / "app" / "google-services.json", "{}\n")
    return root


def test_find_project_root_from_nested_file(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    nested = root / "app" / "src" / "main" / "java" / "com" / "example" / "demo" / "MainActivity.kt"
    assert find_project_root(nested) == root.resolve()
    assert find_project_root(root / "app") == root.resolve()


def test_find_project_root_raises_when_absent(tmp_path: Path) -> None:
    lonely = tmp_path / "nothing" / "here.kt"
    write(lonely, "class A\n")
    with pytest.raises(ProjectError):
        find_project_root(lonely)


def test_priority_order_of_files(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    ctx = scan_project(root)
    assert list(ctx.files) == [
        "settings.gradle.kts",
        "gradle/libs.versions.toml",
        "app/build.gradle.kts",
        "app/src/main/AndroidManifest.xml",
        "app/src/main/java/com/example/demo/MainActivity.kt",
        "app/src/main/res/layout/activity_main.xml",
        "app/src/main/res/values/strings.xml",
    ]
    assert ctx.omitted == []


def test_budget_causes_omissions(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    ctx = scan_project(root, budget_chars=len(SETTINGS))
    assert list(ctx.files) == ["settings.gradle.kts"]
    assert "gradle/libs.versions.toml" in ctx.omitted
    assert "app/src/main/res/values/strings.xml" in ctx.omitted


def test_oversized_file_is_omitted(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    write(root / "app" / "src" / "main" / "res" / "layout" / "big.xml", "x" * 200)
    ctx = scan_project(root, max_file_chars=100)
    assert "app/src/main/res/layout/big.xml" in ctx.omitted
    assert "app/src/main/res/layout/big.xml" not in ctx.files


def test_binary_and_undecodable_files_skipped_silently(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    write(root / "app" / "src" / "main" / "java" / "Bin.kt", b"\x00\x01binary")
    write(root / "app" / "src" / "main" / "java" / "Bad.kt", b"\xff\xfe\xfa")
    ctx = scan_project(root)
    assert "app/src/main/java/Bin.kt" not in ctx.files
    assert "app/src/main/java/Bin.kt" not in ctx.omitted
    assert "app/src/main/java/Bad.kt" not in ctx.files
    assert "app/src/main/java/Bad.kt" not in ctx.omitted


def test_is_text_file(tmp_path: Path) -> None:
    write(tmp_path / "a.kt", "class A\n")
    write(tmp_path / "b.bin", b"\x00abc")
    assert is_text_file(tmp_path / "a.kt")
    assert not is_text_file(tmp_path / "b.bin")


def test_language_kotlin(tmp_path: Path) -> None:
    assert scan_project(make_project(tmp_path)).language == "kotlin"


def test_language_mixed(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    write(root / "app" / "src" / "main" / "java" / "Util.java", "class Util {}\n")
    assert scan_project(root).language == "mixed"


def test_language_java(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "app" / "src" / "main" / "java" / "com" / "example" / "demo" / "MainActivity.kt").unlink()
    write(root / "app" / "src" / "main" / "java" / "Main.java", "class Main {}\n")
    assert scan_project(root).language == "java"


def test_compose_detected(tmp_path: Path) -> None:
    assert scan_project(make_project(tmp_path / "a", compose=True)).uses_compose is True


def test_compose_absent(tmp_path: Path) -> None:
    assert scan_project(make_project(tmp_path / "b", compose=False)).uses_compose is False


def test_namespace_from_build_file(tmp_path: Path) -> None:
    assert scan_project(make_project(tmp_path)).namespace == "com.example.demo"


def test_namespace_falls_back_to_manifest_package(tmp_path: Path) -> None:
    root = make_project(tmp_path, namespace=False)
    write(
        root / "app" / "src" / "main" / "AndroidManifest.xml",
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.legacy">\n</manifest>\n',
    )
    assert scan_project(root).namespace == "com.example.legacy"


def test_tree_excludes_pruned_and_excluded_paths(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    ctx = scan_project(root)
    stripped = {line.strip() for line in ctx.tree.splitlines()}
    assert "app/" in stripped
    assert "MainActivity.kt" in stripped
    for pruned in ("build/", "generated/", ".idea/", "drawable-hdpi/", "X.kt", "x.xml", "icon.png"):
        assert pruned not in stripped
    assert "local.properties" not in stripped
    assert "google-services.json" not in stripped
    assert "local.properties" not in ctx.files
    assert "app/build/generated/X.kt" not in ctx.files


def test_tree_lists_relevant_files_even_when_omitted(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    ctx = scan_project(root, budget_chars=0)
    assert ctx.files == {}
    assert "strings.xml" in {line.strip() for line in ctx.tree.splitlines()}


def test_tree_is_truncated_at_400_lines(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    for i in range(450):
        write(root / "app" / "src" / "main" / "res" / "layout" / f"f{i}.xml", "<a/>\n")
    lines = scan_project(root).tree.splitlines()
    assert len(lines) == 401
    assert lines[-1].startswith("... (") and lines[-1].endswith("more)")


def test_render_context_contains_expected_tags(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    text = render_context(scan_project(root))
    assert text.startswith('<project root="demo" language="kotlin" compose="true" namespace="com.example.demo">\n')
    assert "<tree>\n" in text and "\n</tree>\n" in text
    assert '<file path="settings.gradle.kts">\n' in text
    assert "</file>\n" in text
    assert text.rstrip().endswith("</project>")
    assert "<omitted>" not in text


def test_render_context_includes_omitted(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    text = render_context(scan_project(root, budget_chars=len(SETTINGS)))
    assert "<omitted>gradle/libs.versions.toml\n" in text
    assert "</omitted>\n</project>\n" in text
