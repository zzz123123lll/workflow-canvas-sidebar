"""Exercise npm's real packlist against a small built-release fixture."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from npm_package import pack_release
import npm_package
import build


@pytest.fixture
def release(tmp_path):
    package = {'name': 'dsh-workflow-canvas-fixture', 'version': '0.0.0-test.1',
               'type': 'module', 'main': './index.js', 'license': 'MIT',
               'files': ['index.js', 'cordis.patch.yml', 'server/**', 'runtime/**',
                         'build-info.json', 'dependencies.json', 'asset-manifest.json'],
               'dsh': {'bundle': {'patch': './cordis.patch.yml'}}}
    files = {'package.json': json.dumps(package).encode(), 'index.js': b'export const name="fixture";',
             'cordis.patch.yml': b'plugins: {}\n', 'server/canvas_editor_server.py': b'# fixture\n',
             'runtime/python/python.exe': b'fixture executable',
             'runtime/python/python312.zip': b'fixture standard library',
             'dependencies.json': b'{}\n',
             'build-info.json': b'{"build_id":"fixture-build"}\n'}

    def write(mutate=None):
        entries = dict(files)
        if mutate:
            mutate(entries)
        asset_manifest = {'files': [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                                     'bytes': len(data)} for name, data in entries.items()]}
        entries['asset-manifest.json'] = json.dumps(asset_manifest).encode()
        archive = tmp_path / 'fixture.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for name, data in entries.items():
                z.writestr('workflow-canvas-sidebar/' + name, data)
        manifest = {'build': {'build_id': 'fixture-build'}, 'assets': [
            {'filename': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}]}
        (tmp_path / 'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return entries
    return tmp_path, write


def test_npm_pack_contains_every_built_file_and_records_integrity(release):
    directory, write = release
    entries = write()
    manifest = pack_release(directory)
    archive = directory / manifest['filename']
    assert manifest['sha256'] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest['integrity'].startswith('sha512-')
    with tarfile.open(archive, 'r:gz') as tar:
        packed = {item.name.removeprefix('package/'): tar.extractfile(item).read()
                  for item in tar.getmembers() if item.isfile()}
    assert packed == entries
    assert manifest['build']['build_id'] == 'fixture-build'
    assert manifest['unpacked_bytes'] == sum(map(len, entries.values()))


def test_market_size_limit_checks_unpacked_payload_not_gzip_size(release, monkeypatch):
    directory, write = release
    write(lambda entries: entries.update({'server/large.txt': b'a' * 2048}))
    monkeypatch.setattr(npm_package, 'MAX_UNPACKED_BYTES', 2048)
    with pytest.raises(ValueError, match='解压体积'):
        pack_release(directory)


def test_corrupted_release_is_rejected_before_packing(release):
    directory, write = release
    write()
    with (directory / 'fixture.zip').open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='SHA'):
        pack_release(directory)


def test_npm_packlist_cannot_drop_the_embedded_runtime(release):
    directory, write = release
    def omit_runtime(entries):
        package = json.loads(entries['package.json'])
        package['files'].remove('runtime/**')
        entries['package.json'] = json.dumps(package).encode()
    write(omit_runtime)
    with pytest.raises(ValueError, match='runtime/python/python.exe'):
        pack_release(directory)


def test_market_package_does_not_depend_on_install_hooks(release):
    directory, write = release
    def install_hook(entries):
        package = json.loads(entries['package.json'])
        package['scripts'] = {'postinstall': 'download-runtime'}
        entries['package.json'] = json.dumps(package).encode()
    write(install_hook)
    with pytest.raises(ValueError, match='postinstall'):
        pack_release(directory)


def test_previous_build_metadata_does_not_change_the_source_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    (tmp_path / 'package.json').write_text(json.dumps({'files': [
        'index.js', 'runtime/**', 'build-info.json', 'dependencies.json', 'asset-manifest.json']}), encoding='utf-8')
    (tmp_path / 'index.js').write_text('export const name="fixture";', encoding='utf-8')
    def snapshot():
        return {p.name: hashlib.sha256(build.source_bytes(p)).hexdigest() for p in build.public_files()}
    before = snapshot()
    for name in ('build-info.json', 'dependencies.json', 'asset-manifest.json'):
        (tmp_path / name).write_text('{"previous_build":"generated"}', encoding='utf-8')
    assert snapshot() == before
