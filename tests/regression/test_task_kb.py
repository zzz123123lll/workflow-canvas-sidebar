"""Task-only knowledge access regression tests; never touch the production KB."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def kb(tmp_path, monkeypatch):
    source = (Path(__file__).resolve().parents[2] / "server") / "kb.py"
    spec = importlib.util.spec_from_file_location("task_kb_fixture", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "project"
    knowledge = root / "kb" / "knowledge"
    for name, value in {
        "BASE": root, "KB": knowledge, "SHARED": knowledge / "shared",
        "TASKS": knowledge / "tasks", "INDEX": knowledge / "index.json",
    }.items():
        monkeypatch.setattr(module, name, str(value))
    (knowledge / "shared").mkdir(parents=True)
    (knowledge / "tasks").mkdir()
    (root / "episodes").mkdir()
    return module


def put(kb, relative, text):
    path = Path(kb.KB) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_task_context_is_canonical_and_includes_shared_but_excludes_other_tasks(kb):
    assert kb.ensure_task("task A") == "task-A"
    kb.ensure_task("task B")
    put(kb, "shared/style.md", "# Shared\nSHARED-DO-NOT-USE")
    put(kb, "tasks/task-A/research.md", "# Research\nTASK-A-ONLY")
    put(kb, "tasks/task-B/research.md", "# Research\nTASK-B-DO-NOT-USE")
    entries = kb.list_entries(task="task A")
    assert entries and all(e["task"] == "task-A" and e["scope"] == "task" for e in entries)
    context = Path(kb.context_pack("task A"))
    assert context.name == "task-A-kb-context.md"
    text = context.read_text(encoding="utf-8")
    assert "TASK-A-ONLY" in text
    assert "SHARED-DO-NOT-USE" in text
    assert "TASK-B-DO-NOT-USE" not in text


def test_task_query_reads_only_its_own_files(kb, monkeypatch):
    kb.ensure_task("A")
    kb.ensure_task("B")
    put(kb, "shared/forbidden.md", "SHARED")
    put(kb, "tasks/B/forbidden.md", "OTHER")
    original = kb.title_of
    inspected = []

    def tracking(path):
        inspected.append(str(path))
        return original(path)

    monkeypatch.setattr(kb, "title_of", tracking)
    kb.list_entries(task="A")
    assert inspected
    assert all("tasks" + os.sep + "A" + os.sep in path for path in inspected)


@pytest.mark.parametrize("name", ["", "  ", "..", "../outside", "folder/name", "folder\\name", "C:\\outside", "!!!", "CON", "nul.txt"])
def test_invalid_task_names_are_not_silently_replaced(kb, name):
    with pytest.raises(ValueError):
        kb.ensure_task(name)


@pytest.mark.parametrize("relative", ["../knowledge-neighbor/probe.md", "/absolute.md", "C:/outside.md", "tasks/A/../../shared/style.md", "tasks/A/file.md:secret"])
def test_path_boundaries_reject_escapes(kb, relative):
    with pytest.raises(ValueError):
        kb.safe_rel(relative)


def test_scoped_paths_allow_current_task_only(kb):
    kb.ensure_task("task A")
    own = put(kb, "tasks/task-A/research.md", "OWN")
    assert Path(kb.safe_rel("tasks/task-A/research.md", task="task A")) == own
    for path in ("shared/style.md", "tasks/task-B/research.md", "index.json"):
        with pytest.raises(ValueError):
            kb.safe_rel(path, task="task A")


def test_empty_task_query_never_falls_back_to_global(kb):
    put(kb, "shared/style.md", "SHARED")
    with pytest.raises(ValueError):
        kb.list_entries(task="")


def test_missing_task_context_does_not_fall_back_or_create_a_partition(kb):
    put(kb, "shared/style.md", "SHARED")
    with pytest.raises(ValueError):
        kb.context_pack("missing")
    assert not (Path(kb.TASKS) / "missing").exists()
    assert not (Path(kb.BASE) / "episodes" / "missing-kb-context.md").exists()


def test_new_files_and_full_body_search_are_available_without_reindex(kb):
    kb.ensure_task("A")
    body = "# Ordinary title\n" + "ordinary filler " * 100 + "尾部独有标记"
    put(kb, "tasks/A/new-note.md", body)
    hits = kb.list_entries(task="A", search="尾部独有标记")
    assert [e["path"] for e in hits] == ["tasks/A/new-note.md"]
    assert "尾部独有标记" in Path(kb.context_pack("A")).read_text(encoding="utf-8")


def test_sync_index_does_not_follow_external_junction(kb, tmp_path):
    kb.ensure_task("A")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("OUTSIDE", encoding="utf-8")
    link = Path(kb.TASKS) / "A" / "linked"
    if sys.platform == "win32":
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip("cannot create junction in test environment")
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        kb.safe_rel("tasks/A/linked/secret.md", task="A")
    assert not any(e["path"].endswith("secret.md") for e in kb.sync_index())


def test_partition_root_cannot_point_at_another_task(kb):
    kb.ensure_task("B")
    link = Path(kb.TASKS) / "A"
    if sys.platform == "win32":
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(Path(kb.TASKS) / "B")], capture_output=True)
        if result.returncode:
            pytest.skip("cannot create junction in test environment")
    else:
        link.symlink_to(Path(kb.TASKS) / "B", target_is_directory=True)
    with pytest.raises(ValueError):
        kb.list_entries(task="A")


def test_archive_copies_current_markdown_outputs_only_for_current_task(kb):
    kb.ensure_task("demo")
    episodes = Path(kb.BASE) / "episodes"
    for filename in ("demo-script.md", "demo-qa.md", "demo-publish.md", "demo-review.md", "other-script.md"):
        (episodes / filename).write_text(filename, encoding="utf-8")
    result = kb.archive("demo")
    assert set(result["copied"]) >= {"demo-script.md", "demo-qa.md", "demo-publish.md", "demo-review.md"}
    assert "other-script.md" not in result["copied"]


def test_explicit_archive_rejects_source_outside_project(kb, tmp_path):
    kb.ensure_task("A")
    external = tmp_path / "outside.md"
    external.write_text("PRIVATE", encoding="utf-8")
    with pytest.raises(ValueError):
        kb.archive("A", paths=[str(external)])


def test_explicit_archive_cannot_import_another_knowledge_partition(kb):
    kb.ensure_task("A")
    kb.ensure_task("B")
    put(kb, "tasks/B/private.md", "OTHER-TASK")
    with pytest.raises(ValueError):
        kb.archive("A", paths=["kb/knowledge/tasks/B/private.md"])


def test_archive_rejects_ambiguous_output_names_before_copying(kb):
    kb.ensure_task("A")
    for folder in ("first", "second"):
        path = Path(kb.BASE) / folder / "result.md"
        path.parent.mkdir()
        path.write_text(folder, encoding="utf-8")
    with pytest.raises(ValueError):
        kb.archive("A", paths=["first/result.md", "second/result.md"])
    assert not (Path(kb.TASKS) / "A" / "outputs" / "result.md").exists()


def test_partition_listing_returns_only_metadata(kb):
    kb.ensure_task("task A")
    kb.ensure_task("task B")
    rows = kb.list_tasks()
    assert {r["task_id"] for r in rows} == {"task-A", "task-B"}
    assert all(set(r) <= {"task_id", "slug", "name", "created", "updated"} for r in rows)


def test_cli_read_and_write_accept_task_scope(kb, monkeypatch, capsys):
    kb.ensure_task("A")
    monkeypatch.setattr(sys, "argv", ["kb.py", "write", "tasks/A/note.md", "--task", "A", "--text", "OWN"])
    kb.main()
    assert json.loads(capsys.readouterr().out)["ok"]
    monkeypatch.setattr(sys, "argv", ["kb.py", "read", "tasks/A/note.md", "--task", "A"])
    kb.main()
    assert capsys.readouterr().out.strip() == "OWN"


def test_scoped_cli_import_cannot_read_another_kb_partition(kb, monkeypatch):
    kb.ensure_task("A")
    kb.ensure_task("B")
    other = put(kb, "tasks/B/private.md", "OTHER-TASK")
    monkeypatch.setattr(sys, "argv", ["kb.py", "write", "tasks/A/import.md", "--task", "A", "--file", str(other)])
    with pytest.raises(ValueError):
        kb.main()
    assert not (Path(kb.TASKS) / "A" / "import.md").exists()
