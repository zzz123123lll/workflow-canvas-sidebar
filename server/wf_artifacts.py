"""Verified directory artifacts for workflow projects and task media assets.

The caller supplies roots already checked against its run/project scope. This
module additionally refuses links, Windows reparse points, path traversal, and
overlapping trees. Replaced destinations are retained as sibling backups.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _root(path):
    value = os.fspath(path)
    if not value or ".." in value.replace("\\", "/").split("/"):
        raise ValueError("Invalid directory artifact path")
    absolute = os.path.abspath(value)
    # Keep all internal I/O extended-length, including temp/backup siblings and
    # descendant files. Normal Windows paths can fail well before final outputs
    # exceed MAX_PATH because the transactional suffix adds another 37+ chars.
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    full = Path(absolute)
    if full == full.parent:
        raise ValueError("A filesystem root cannot be a directory artifact")
    for current in reversed((full, *full.parents)):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise ValueError("Links are not permitted in directory artifacts")
    return full


def _inside(path, root):
    try:
        return os.path.normcase(os.path.commonpath([str(path), str(root)])) == os.path.normcase(str(root))
    except ValueError:
        return False


def _replace_bounded(source, destination):
    """Tolerate brief Windows sharing/access locks without relaxing permissions."""
    for attempt in range(6):
        _root(source)
        _root(destination)
        try:
            return os.replace(source, destination)
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32) or attempt == 5:
                raise
            time.sleep(.05 * (2 ** attempt))


def _scan(root):
    files, directories = [], []
    times = [root.stat()]

    def visit(directory):
        initial_directory = directory.stat()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
        for entry in entries:
            path = Path(entry.path)
            if not _inside(path, root):
                raise ValueError("Directory artifact escapes its root")
            # Windows DirEntry metadata can omit file identities; lstat supplies them.
            info = path.lstat()
            if _is_link(info):
                raise ValueError("Links are not permitted in directory artifacts")
            relative = path.relative_to(root).as_posix()
            times.append(info)
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                with path.open("rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if _is_link(opened) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError("Directory artifact changed while reading")
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    after = os.fstat(handle.fileno())
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                    raise ValueError("Directory artifact changed while reading")
                times.append(after)
                files.append({"path": relative, "sha256": digest, "bytes": after.st_size,
                              "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns})
            else:
                raise ValueError("Only regular files and directories can be artifacts")
        final_directory = directory.stat()
        if (initial_directory.st_mtime_ns, initial_directory.st_ctime_ns) != (final_directory.st_mtime_ns, final_directory.st_ctime_ns):
            raise ValueError("Directory artifact tree changed while reading")

    visit(root)
    if not files:
        return None
    content = {"files": [{key: row[key] for key in ("path", "sha256", "bytes")} for row in files],
               "directories": directories}
    digest = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"kind": "directory", "sha256": digest, "bytes": sum(row["bytes"] for row in files),
            "mtime_ns": max(info.st_mtime_ns for info in times), "ctime_ns": max(info.st_ctime_ns for info in times),
            "files": files, "directories": directories}


def directory_evidence(path):
    """Return complete-tree evidence, None for missing/empty/non-directory output."""
    root = _root(path)
    if not root.is_dir():
        return None
    return _scan(root)


def copy_directory(source, destination):
    """Install a verified full-tree copy; preserve prior destination as a backup."""
    source, destination = _root(source), _root(destination)
    if _inside(source, destination) or _inside(destination, source):
        raise ValueError("Source and destination directory artifacts overlap")
    evidence = directory_evidence(source)
    if evidence is None:
        raise ValueError("Directory artifact is missing or empty")
    if destination.exists() and not destination.is_dir():
        raise ValueError("Directory artifact destination is a regular file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp-" + uuid.uuid4().hex)
    backup = destination.with_name(destination.name + ".backup-" + uuid.uuid4().hex)
    moved_old = False
    try:
        temporary.mkdir()
        for relative in evidence["directories"]:
            (temporary / relative).mkdir(parents=True, exist_ok=True)
        for entry in evidence["files"]:
            src = _root(source / entry["path"])
            if not _inside(src, source) or not src.is_file():
                raise ValueError("Directory artifact changed before copying")
            dst = temporary / entry["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied = directory_evidence(temporary)
        final_source = directory_evidence(source)
        if not copied or not final_source or copied["sha256"] != evidence["sha256"] or final_source["sha256"] != evidence["sha256"]:
            raise ValueError("Directory artifact changed during copying")
        _root(destination)
        if destination.exists():
            _replace_bounded(destination, backup)
            moved_old = True
        try:
            _replace_bounded(temporary, destination)
        except Exception:
            if moved_old and not destination.exists():
                _replace_bounded(backup, destination)
                moved_old = False
            raise
    finally:
        # Only our freshly generated sibling temp is eligible for recursive cleanup.
        if temporary.exists():
            checked = _root(temporary)
            if checked.parent != destination.parent or not checked.name.startswith(destination.name + ".tmp-"):
                raise ValueError("Unsafe temporary artifact cleanup")
            shutil.rmtree(checked)
