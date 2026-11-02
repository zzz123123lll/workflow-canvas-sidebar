"""Load pinned, explicitly bound skills into one workflow node's workspace.

This module copies declared bytes and returns instructions/provenance. It never
downloads skills, executes skill scripts, installs dependencies, or discovers
other task knowledge. The caller decides which skills the node is authorized to
use and treats their text as task guidance subordinate to its execution scope.
"""
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile


BASE = Path(os.environ.get("CANVAS_HOME") or Path(__file__).resolve().parent)
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def _filesystem_path(value):
    """Use Windows long-path syntax for all internal I/O, without OS changes."""
    absolute = os.path.abspath(os.fspath(value))
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        absolute = "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute
    return Path(absolute)


def _component(value):
    if (not value or value in {".", ".."} or value.endswith((".", " "))
            or any(ord(c) < 32 for c in value) or any(c in value for c in '\\/:<>"|?*')
            or _RESERVED.match(value)):
        raise ValueError("Invalid skill path component")
    return value


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("Invalid skill identifier")
    return _component(value)


def _relative(value):
    if not isinstance(value, str) or not value or ntpath.splitdrive(value)[0]:
        raise ValueError("Skill path must be relative")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError("Noncanonical skill path")
    for component in path.parts:
        _component(component)
    return path.parts


def _is_link(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _checked_path(root, parts):
    """Reject every link/reparse component, including existing destination parents."""
    current = root
    if _is_link(current):
        raise ValueError("Skill links/junctions are not allowed")
    for part in parts:
        if current.exists() and not current.is_dir():
            raise ValueError("Skill path parent is not a directory")
        current = current / part
        if _is_link(current):
            raise ValueError("Skill links/junctions are not allowed")
    try:
        within = os.path.normcase(os.path.commonpath([str(root.resolve()), str(current.resolve())])) == os.path.normcase(str(root.resolve()))
    except (ValueError, OSError) as exc:
        raise ValueError("Invalid skill path") from exc
    if not within:
        raise ValueError("Skill path escapes its trusted directory")
    return current


def _read_registry(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Skill registry is missing or invalid") from exc
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise ValueError("Skill registry must contain a skills list")
    entries = {}
    for entry in data["skills"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid skill registry entry")
        identity = _identity(entry.get("id"))
        if identity.casefold() in entries:
            raise ValueError("Duplicate skill registry identifier")
        entries[identity.casefold()] = entry
    return entries


def _check_existing_destination(destination, declared):
    if not destination.exists():
        return
    if not destination.is_dir():
        raise ValueError("Skill staging destination is not a directory")
    for directory, dirs, names in os.walk(destination, followlinks=False):
        for name in dirs + names:
            candidate = Path(directory) / name
            if _is_link(candidate):
                raise ValueError("Skill destination links/junctions are not allowed")
        for name in names:
            candidate = Path(directory) / name
            relative = candidate.relative_to(destination).as_posix()
            if relative not in declared or not stat.S_ISREG(candidate.lstat().st_mode):
                raise ValueError("Unexpected unpinned file in skill destination")


def _atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".skill-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def stage_skills(skill_ids, workspace, *, registry_path=None, base=None):
    """Stage only selected, SHA-256-pinned bundles; return full entry instructions.

    Registry ``root`` paths must start with ``skills/`` under ``base``. Every
    file, including ``entry``, must be in ``files_sha256``; commit is a full
    Git SHA. Validation of every selected bundle completes before any writes.
    Existing unpinned destination files cause refusal rather than silent reuse.
    """
    if not isinstance(skill_ids, list):
        raise ValueError("Skill bindings must be a list")
    if not skill_ids:
        return []
    identities = [_identity(identity) for identity in skill_ids]
    if len({identity.casefold() for identity in identities}) != len(identities):
        raise ValueError("Duplicate skill binding")
    project = _filesystem_path(base if base is not None else BASE).resolve()
    destination_root = _filesystem_path(workspace)
    # An explicitly supplied workspace root must not itself redirect elsewhere.
    if _is_link(destination_root):
        raise ValueError("Workspace links/junctions are not allowed")
    registry = _read_registry(_filesystem_path(registry_path) if registry_path is not None else project / "kb/skill-registry.json")
    prepared = []
    total_bytes = 0
    for identity in identities:
        entry = registry.get(identity.casefold())
        if entry is None or entry["id"] != identity:
            raise ValueError("Unknown skill binding: " + identity)
        root_parts = _relative(entry.get("root"))
        if len(root_parts) < 2 or root_parts[0] != "skills":
            raise ValueError("Skill source must be under the project skills tree")
        source_root = _checked_path(project, root_parts)
        manifest = entry.get("files_sha256")
        main_name = entry.get("entry", "SKILL.md")
        main_parts = _relative(main_name)
        if not isinstance(manifest, dict) or not manifest or len(manifest) > MAX_FILES or main_name not in manifest:
            raise ValueError("Skill entry must be declared in a bounded files_sha256 manifest")
        commit = entry.get("commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", commit):
            raise ValueError("Skill commit must be pinned to a full Git SHA")
        destination = _checked_path(destination_root, ("skills", identity))
        payloads = {}
        names = set()
        for relative, expected_hash in manifest.items():
            parts = _relative(relative)
            if relative.casefold() in names:
                raise ValueError("Case-colliding skill manifest paths")
            names.add(relative.casefold())
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
                raise ValueError("Invalid skill SHA-256 manifest")
            source = _checked_path(source_root, parts)
            target = _checked_path(destination, parts)
            if target.exists() and not stat.S_ISREG(target.lstat().st_mode):
                raise ValueError("Skill destination must be a regular file")
            try:
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ValueError("Skill source must be a bounded regular file")
                payload = source.read_bytes()
            except OSError as exc:
                raise ValueError("Missing skill file: " + relative) from exc
            if len(payload) > MAX_FILE_BYTES or hashlib.sha256(payload).hexdigest() != expected_hash.lower():
                raise ValueError("Skill SHA-256 hash verification failed: " + relative)
            total_bytes += len(payload)
            if total_bytes > MAX_BUNDLE_BYTES:
                raise ValueError("Selected skill bundles exceed the staging budget")
            payloads[relative] = payload
        _check_existing_destination(destination, manifest)
        try:
            instructions = payloads[main_name].decode("utf-8-sig").replace("\r\n", "\n")
        except UnicodeDecodeError as exc:
            raise ValueError("Skill entry must be UTF-8 text") from exc
        if not instructions.strip():
            raise ValueError("Skill entry instructions are empty")
        metadata = {"id": identity, "name": entry.get("name", identity),
                    "source_url": entry.get("source_url", ""), "commit": commit,
                    "license": entry.get("license", ""), "requirements": entry.get("requirements", []),
                    "entry": PurePosixPath("skills", identity, *main_parts).as_posix(),
                    "sha256": manifest[main_name].lower(), "files_sha256": dict(manifest),
                    "instructions": instructions}
        prepared.append((destination, payloads, metadata))
    for destination, payloads, _ in prepared:
        for relative, payload in payloads.items():
            target = _checked_path(destination, _relative(relative))
            _atomic_bytes(target, payload)
    return [metadata for _, _, metadata in prepared]
