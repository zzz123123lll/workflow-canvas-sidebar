"""Build a normalized Windows ZIP from source allowlists and official locked inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

from runtime import assemble

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_bytes(path):
    raw = path.read_bytes()
    relative = path.relative_to(ROOT)
    if relative.parts[0] == 'third_party':
        return raw  # Original notice bytes retain their pinned upstream hashes.
    if path.suffix.lower() in {'.js','.mjs','.cjs','.py','.ps1','.json','.md','.txt','.yml','.yaml','.html','.cmd'}:
        raw = raw.removeprefix(b'\xef\xbb\xbf').replace(b'\r\n', b'\n')
        if path.suffix.lower() == '.ps1':
            raw = b'\xef\xbb\xbf' + raw.replace(b'\n', b'\r\n')
    return raw


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')


def public_files():
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    paths = {ROOT / 'package.json'}
    for pattern in package['files']:
        if pattern.startswith('runtime/') or pattern in {'build-info.json', 'dependencies.json', 'asset-manifest.json'}:
            continue
        if pattern.endswith('/**'):
            paths.update(p for p in (ROOT / pattern[:-3]).rglob('*') if p.is_file())
        else:
            paths.add(ROOT / pattern)
    return sorted(p for p in paths if p.is_file() and not any(v in p.parts for v in
        ['__pycache__', '.pytest_cache', '.git', 'node_modules']) and p.suffix not in {'.pyc', '.pyo'}
        and p.name not in {'canvas-home.json', '.gitignore'} and not p.relative_to(ROOT).as_posix().startswith('docs/v0.16.0/'))


def validate_public_tree(directory):
    forbidden_names = {'canvas-home.json', 'host-agent.json', 'api-instance.json', '.env'}
    for file in directory.rglob('*'):
        if not file.is_file():
            continue
        rel = file.relative_to(directory)
        if file.is_symlink() or any(v in rel.parts for v in ['.runs', '.dsh', '.git', '__pycache__']) or file.name in forbidden_names:
            raise ValueError('发行包含私有文件或链接：' + str(rel))
        if file.suffix in {'.json', '.js', '.py', '.md', '.yml', '.txt', '.ps1'} and file.stat().st_size < 2 * 1024 * 1024:
            text = file.read_text(encoding='utf-8-sig', errors='replace')
            # Concrete credential shapes, not labels, placeholder documentation or account-free test fixtures.
            if re.search(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9]{32,}', text):
                raise ValueError('发行文件疑似包含凭据：' + str(rel))


def build(output, cache, offline=False):
    output.mkdir(parents=True, exist_ok=True)
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    sources = public_files()
    source_manifest = [{'path': p.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(source_bytes(p)).hexdigest()} for p in sources]
    source_digest = hashlib.sha256(json.dumps(source_manifest, sort_keys=True).encode()).hexdigest()
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    identity = {'version': package['version'], 'build_id': source_digest[:20], 'source_sha256': source_digest,
                'source_commit': commit.stdout.strip() if commit.returncode == 0 else None, 'schema': 2,
                'platform': 'windows-x64', 'zip_metadata': '1980-01-01T00:00:00, normalized paths and permissions',
                'source_normalization': 'Owned text uses LF; PowerShell uses UTF-8 BOM/CRLF; upstream notices remain byte-exact.'}
    with tempfile.TemporaryDirectory(prefix='workflow-canvas-build-') as temp:
        staging = Path(temp) / 'workflow-canvas-sidebar'; staging.mkdir()
        for source in sources:
            target = staging / source.relative_to(ROOT); target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source_bytes(source))
        lock = assemble(staging / 'runtime/python', cache, offline)
        write_json(staging / 'build-info.json', identity)
        write_json(staging / 'dependencies.json', lock)
        validate_public_tree(staging)
        files = [{'path': p.relative_to(staging).as_posix(), 'sha256': sha(p), 'bytes': p.stat().st_size}
                 for p in sorted(staging.rglob('*')) if p.is_file()]
        write_json(staging / 'asset-manifest.json', {'build': identity, 'files': files})
        archive = output / ('workflow-canvas-sidebar-v' + package['version'] + '-windows-x64.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for file in sorted(staging.rglob('*')):
                if file.is_file():
                    name = 'workflow-canvas-sidebar/' + file.relative_to(staging).as_posix()
                    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    with file.open('rb') as inp, z.open(entry, 'w', force_zip64=True) as dst:
                        shutil.copyfileobj(inp, dst, 1024 * 1024)
        with zipfile.ZipFile(archive) as z:
            if z.testzip() is not None:
                raise ValueError('发行ZIP损坏')
        manifest = {'build': identity, 'assets': [{'filename': archive.name, 'sha256': sha(archive), 'bytes': archive.stat().st_size}],
                    'files': files, 'dependencies_sha256': sha(ROOT / 'dependencies.lock.json')}
        write_json(output / 'release-manifest.json', manifest)
        write_json(output / 'dependencies.json', lock)
        for name in ['THIRD_PARTY_NOTICES.md', 'RELEASE_NOTES.md']:
            if (ROOT / name).exists():shutil.copyfile(ROOT / name, output / name)
        sums = [sha(p) + '  ' + p.name for p in sorted(output.iterdir()) if p.is_file() and p.name != 'SHA256SUMS.txt']
        (output / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n', encoding='utf-8', newline='\n')
        print(json.dumps({'archive': str(archive), 'build': identity, 'files': len(files), 'sha256': sha(archive)}, ensure_ascii=False))
        return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--cache', type=Path, default=ROOT / '.cache/dependencies')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    build(args.output.resolve(), args.cache.resolve(), args.offline)
