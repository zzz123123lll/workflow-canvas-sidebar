"""Pinned skill staging contracts; all writes stay in disposable test directories."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


SOURCE = (Path(__file__).resolve().parents[2] / "server")


@pytest.fixture
def bundle(tmp_path):
    spec = importlib.util.spec_from_file_location("isolated_wf_skills", SOURCE / "wf_skills.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "project"
    skill = root / "skills/vendor/official-demo"
    skill.mkdir(parents=True)
    text = "---\nname: demo\n---\n# Production guidance\nRead references/timing.md.\n"
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    (skill / "references").mkdir()
    (skill / "references/timing.md").write_text("Use measured timing.", encoding="utf-8")
    (skill / "not-declared.txt").write_text("DO_NOT_COPY", encoding="utf-8")
    registry = root / "kb/skill-registry.json"
    registry.parent.mkdir()
    entry = {"id": "official-demo", "name": "Demo skill", "root": "skills/vendor/official-demo",
             "entry": "SKILL.md", "source_url": "https://github.com/example/demo/tree/" + "a" * 40,
             "commit": "a" * 40, "license": "MIT", "requirements": ["Python"],
             "files_sha256": {name: hashlib.sha256((skill / name).read_bytes()).hexdigest()
                              for name in ("SKILL.md", "references/timing.md")}}
    registry.write_text(json.dumps({"skills": [entry]}), encoding="utf-8")
    workspace = tmp_path / "workspace"
    return module, root, skill, registry, entry, workspace, text


def save(registry, entry):
    registry.write_text(json.dumps({"skills": [entry]}), encoding="utf-8")


def test_stage_loads_real_pinned_main_and_reference_only(bundle):
    module, root, skill, registry, _, workspace, text = bundle
    result = module.stage_skills(["official-demo"], workspace, base=root, registry_path=registry)
    assert len(result) == 1
    row = result[0]
    assert row["id"] == "official-demo"
    assert row["entry"] == "skills/official-demo/SKILL.md"
    assert row["instructions"] == text
    assert row["commit"] == "a" * 40
    assert row["license"] == "MIT"
    assert row["requirements"] == ["Python"]
    assert row["sha256"] == hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest()
    assert (workspace / row["entry"]).read_text(encoding="utf-8") == text
    assert (workspace / "skills/official-demo/references/timing.md").read_text(encoding="utf-8") == "Use measured timing."
    assert not (workspace / "skills/official-demo/not-declared.txt").exists()


def test_no_bindings_do_not_require_registry(tmp_path):
    spec = importlib.util.spec_from_file_location("empty_wf_skills", SOURCE / "wf_skills.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.stage_skills([], tmp_path / "workspace", base=tmp_path / "no-project") == []


@pytest.mark.parametrize("identity", ["../demo", "a/b", "C:/demo", "", "CON", "demo.", "demo "])
def test_invalid_binding_ids_are_rejected(bundle, identity):
    module, root, _, registry, _, workspace, _ = bundle
    with pytest.raises(ValueError):
        module.stage_skills([identity], workspace, base=root, registry_path=registry)


def test_unknown_skill_and_duplicate_bindings_are_rejected(bundle):
    module, root, _, registry, _, workspace, _ = bundle
    for identities in (["missing"], ["official-demo", "official-demo"]):
        with pytest.raises(ValueError):
            module.stage_skills(identities, workspace, base=root, registry_path=registry)


@pytest.mark.parametrize("root_value", ["../outside", "kb/knowledge/tasks/other", "skills/../outside", "C:/outside", "skills./vendor/demo"])
def test_skill_source_must_stay_in_trusted_skills_tree(bundle, root_value):
    module, root, _, registry, entry, workspace, _ = bundle
    entry["root"] = root_value
    save(registry, entry)
    with pytest.raises(ValueError):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)


@pytest.mark.parametrize("path", ["../outside.md", "/absolute.md", "C:/absolute.md", "references/../../outside.md", "references./timing.md", "SKILL.md:ads"])
def test_manifest_paths_cannot_escape_or_use_windows_aliases(bundle, path):
    module, root, _, registry, entry, workspace, _ = bundle
    entry["files_sha256"][path] = "a" * 64
    save(registry, entry)
    with pytest.raises(ValueError):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)
    assert not workspace.exists(), "validate the complete bundle before writing"


def test_hash_tampering_fails_without_partially_staging(bundle):
    module, root, skill, registry, entry, workspace, _ = bundle
    (skill / "references/timing.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)hash|sha|校验"):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)
    assert not workspace.exists()


def test_entry_must_be_declared_and_commit_pinned(bundle):
    module, root, _, registry, entry, workspace, _ = bundle
    for mutate in (lambda e: e["files_sha256"].pop("SKILL.md"), lambda e: e.update(commit="main")):
        copy = json.loads(json.dumps(entry))
        mutate(copy)
        save(registry, copy)
        with pytest.raises(ValueError):
            module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)


def make_directory_link(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if result.returncode:
            pytest.skip("directory links unavailable")


def test_source_junction_is_rejected_even_if_target_stays_in_project(bundle):
    module, root, skill, registry, entry, workspace, _ = bundle
    link = root / "skills/vendor/linked"
    make_directory_link(link, skill)
    entry["root"] = "skills/vendor/linked"
    save(registry, entry)
    with pytest.raises(ValueError, match="(?i)link|junction|链接"):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)


def test_destination_junction_cannot_write_into_other_task(bundle, tmp_path):
    module, root, _, registry, entry, workspace, _ = bundle
    workspace.mkdir()
    foreign = tmp_path / "other-task"
    foreign.mkdir()
    make_directory_link(workspace / "skills", foreign)
    with pytest.raises(ValueError, match="(?i)link|junction|链接"):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)
    assert not list(foreign.iterdir())


def test_all_requested_skills_validated_before_any_copy(bundle):
    module, root, _, registry, entry, workspace, _ = bundle
    with pytest.raises(ValueError):
        module.stage_skills([entry["id"], "missing-second"], workspace, base=root, registry_path=registry)
    assert not workspace.exists()


def test_unselected_registry_skill_is_not_staged(bundle):
    module, root, _, registry, entry, workspace, _ = bundle
    other = root / "skills/vendor/other"
    other.mkdir()
    (other / "SKILL.md").write_text("OTHER_SKILL_MARKER", encoding="utf-8")
    second = dict(entry, id="other", root="skills/vendor/other", files_sha256={
        "SKILL.md": hashlib.sha256((other / "SKILL.md").read_bytes()).hexdigest()})
    registry.write_text(json.dumps({"skills": [entry, second]}), encoding="utf-8")
    result = module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)
    assert len(result) == 1
    assert not (workspace / "skills/other").exists()
    assert "OTHER_SKILL_MARKER" not in json.dumps(result)


def test_unpinned_leftover_file_is_not_silently_reused(bundle):
    module, root, _, registry, entry, workspace, _ = bundle
    module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)
    leftover = workspace / "skills/official-demo/old-script.py"
    leftover.write_text("print('old')", encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)unpinned"):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)


def test_existing_directory_at_declared_destination_is_a_validation_error(bundle):
    module, root, _, registry, entry, workspace, _ = bundle
    destination = workspace / "skills/official-demo/SKILL.md"
    destination.mkdir(parents=True)
    with pytest.raises(ValueError):
        module.stage_skills([entry["id"]], workspace, base=root, registry_path=registry)
    assert not (workspace / "skills/official-demo/references").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length filesystem contract")
def test_long_workspace_stages_pinned_skill_without_os_setting_changes(bundle):
    module, root, _, registry, entry, workspace, text = bundle
    long_workspace = workspace / ("generation-" + "a" * 70) / ("node-" + "b" * 70) / ("task-" + "c" * 70)
    assert len(str(long_workspace / "skills/official-demo/references/timing.md")) > 300
    result = module.stage_skills([entry["id"]], long_workspace, base=root, registry_path=registry)
    extended_workspace = Path("\\\\?\\" + str(long_workspace.absolute()))
    assert (extended_workspace / result[0]["entry"]).read_text(encoding="utf-8") == text
    assert (extended_workspace / "skills/official-demo/references/timing.md").read_text(encoding="utf-8") == "Use measured timing."
    assert not (extended_workspace / "skills/official-demo/not-declared.txt").exists()
