"""Directory artifacts are verified and copied only within disposable test trees."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location("isolated_wf_artifacts", (Path(__file__).resolve().parents[2] / "server") / "wf_artifacts.py")
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


def make_tree(root):
    (root / "src").mkdir(parents=True)
    (root / "src/scene.tsx").write_text("scene", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets/data.json").write_text('{"count":2}', encoding="utf-8")
    return root


def test_directory_evidence_hashes_full_regular_tree(tmp_path):
    source = make_tree(tmp_path / "source")
    evidence = artifacts.directory_evidence(source)
    assert evidence["kind"] == "directory"
    assert evidence["bytes"] == 16
    assert {entry["path"] for entry in evidence["files"]} == {"src/scene.tsx", "assets/data.json"}
    assert all(len(entry["sha256"]) == 64 for entry in evidence["files"])
    assert artifacts.directory_evidence(source) == evidence
    (source / "assets/data.json").write_text('{"count":3}', encoding="utf-8")
    assert artifacts.directory_evidence(source)["sha256"] != evidence["sha256"]


def test_missing_empty_or_plain_file_is_not_directory_output(tmp_path):
    assert artifacts.directory_evidence(tmp_path / "missing") is None
    (tmp_path / "empty").mkdir()
    assert artifacts.directory_evidence(tmp_path / "empty") is None
    (tmp_path / "plain").write_text("x", encoding="utf-8")
    assert artifacts.directory_evidence(tmp_path / "plain") is None


def test_copy_replaces_target_without_stale_files_and_keeps_old_version(tmp_path):
    source = make_tree(tmp_path / "source")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("history", encoding="utf-8")
    artifacts.copy_directory(source, destination)
    assert not (destination / "old.txt").exists()
    assert (destination / "src/scene.tsx").read_text(encoding="utf-8") == "scene"
    assert artifacts.directory_evidence(destination)["sha256"] == artifacts.directory_evidence(source)["sha256"]
    backups = list(tmp_path.glob("destination.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text(encoding="utf-8") == "history"


def link_directory(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)


def test_evidence_and_copy_reject_external_junctions_without_reading_them(tmp_path):
    source = make_tree(tmp_path / "source")
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("EXTERNAL", encoding="utf-8")
    link_directory(source / "foreign", external)
    with pytest.raises(ValueError):
        artifacts.directory_evidence(source)
    with pytest.raises(ValueError):
        artifacts.copy_directory(source, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_copy_rejects_destination_junction_without_changing_target(tmp_path):
    source = make_tree(tmp_path / "source")
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("untouched", encoding="utf-8")
    link_directory(tmp_path / "destination", external)
    with pytest.raises(ValueError):
        artifacts.copy_directory(source, tmp_path / "destination")
    assert (external / "private.txt").read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("relative", ["source", "source/nested", "source/../destination"])
def test_copy_refuses_overlapping_or_traversal_paths(tmp_path, relative):
    source = make_tree(tmp_path / "source")
    with pytest.raises(ValueError):
        artifacts.copy_directory(source, tmp_path / relative)


def test_failed_swap_restores_original_target(tmp_path, monkeypatch):
    source = make_tree(tmp_path / "source")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("keep", encoding="utf-8")
    original = artifacts.os.replace

    def fail_install(src, dst):
        if ".tmp-" in str(src) and Path(dst) == artifacts._root(destination):
            raise OSError("simulated interrupted install")
        return original(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", fail_install)
    with pytest.raises(OSError):
        artifacts.copy_directory(source, destination)
    assert (destination / "old.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob("destination.tmp-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH regression")
def test_long_directory_paths_support_evidence_replace_backup_and_source_copy(tmp_path):
    source = make_tree(tmp_path / "source")
    parent = tmp_path
    for index in range(4):
        parent = parent / ("nested-" + str(index) + "-" + "a" * 60)
    destination = parent / "project"
    assert len(str(destination)) > 300
    artifacts.copy_directory(source, destination)
    assert artifacts.directory_evidence(destination)["sha256"] == artifacts.directory_evidence(source)["sha256"]
    (source / "src/scene.tsx").write_text("updated scene", encoding="utf-8")
    artifacts.copy_directory(source, destination)
    actual = Path("\\\\?\\" + str(destination))
    assert (actual / "src/scene.tsx").read_text(encoding="utf-8") == "updated scene"
    backups = list(actual.parent.glob("project.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "src/scene.tsx").read_text(encoding="utf-8") == "scene"
    artifacts.copy_directory(destination, parent / "second-project")
    assert artifacts.directory_evidence(parent / "second-project")["sha256"] == artifacts.directory_evidence(destination)["sha256"]


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation regression")
@pytest.mark.parametrize("winerror", [5, 32])
def test_copy_retries_transient_windows_install_lock(tmp_path, monkeypatch, winerror):
    source = make_tree(tmp_path / "source")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("history", encoding="utf-8")
    original_replace = artifacts.os.replace
    attempts = []
    delays = []

    def replace(src, dst):
        if ".tmp-" in str(src):
            attempts.append(str(src))
            if len(attempts) <= 2:
                error = PermissionError("transient Windows scanner/file handle lock")
                error.winerror = winerror
                raise error
        return original_replace(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", replace)
    monkeypatch.setattr(artifacts.time, "sleep", delays.append)
    artifacts.copy_directory(source, destination)
    assert len(attempts) == 3
    assert len(delays) == 2
    assert (destination / "src/scene.tsx").read_text(encoding="utf-8") == "scene"
    assert len(list(tmp_path.glob("destination.backup-*"))) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation regression")
def test_permanent_windows_lock_is_bounded_and_restores_original_target(tmp_path, monkeypatch):
    source = make_tree(tmp_path / "source")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("keep", encoding="utf-8")
    original_replace = artifacts.os.replace
    attempts = []

    def replace(src, dst):
        if ".tmp-" in str(src):
            attempts.append(str(src))
            error = PermissionError("permanent Windows lock")
            error.winerror = 5
            raise error
        return original_replace(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", replace)
    monkeypatch.setattr(artifacts.time, "sleep", lambda delay: None)
    with pytest.raises(PermissionError):
        artifacts.copy_directory(source, destination)
    assert 2 <= len(attempts) <= 8
    assert (destination / "old.txt").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob("destination.tmp-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation regression")
@pytest.mark.parametrize("phase", ["backup", "restore"])
def test_windows_lock_retry_also_protects_backup_and_rollback(tmp_path, monkeypatch, phase):
    source = make_tree(tmp_path / "source")
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.txt").write_text("keep", encoding="utf-8")
    original_replace = artifacts.os.replace
    locked_attempts = []

    def replace(src, dst):
        if phase == "restore" and ".tmp-" in str(src):
            raise OSError("install failed; original must be restored")
        selected_phase = ((phase == "backup" and ".backup-" in str(dst))
                          or (phase == "restore" and ".backup-" in str(src)))
        if selected_phase:
            locked_attempts.append(str(src))
            if len(locked_attempts) <= 2:
                error = PermissionError("transient Windows lock")
                error.winerror = 32
                raise error
        return original_replace(src, dst)

    monkeypatch.setattr(artifacts.os, "replace", replace)
    monkeypatch.setattr(artifacts.time, "sleep", lambda delay: None)
    if phase == "restore":
        with pytest.raises(OSError, match="install failed"):
            artifacts.copy_directory(source, destination)
        assert (destination / "old.txt").read_text(encoding="utf-8") == "keep"
    else:
        artifacts.copy_directory(source, destination)
        assert (destination / "src/scene.tsx").read_text(encoding="utf-8") == "scene"
    assert len(locked_attempts) == 3
    assert not list(tmp_path.glob("destination.tmp-*"))
