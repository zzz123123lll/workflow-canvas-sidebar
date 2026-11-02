"""Pack the verified, complete Windows build for npm; never pack a developer home."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from build import validate_public_tree, write_json
from runtime import extract

MAX_UNPACKED_BYTES = 256 * 1024 * 1024


def digest(path, algorithm='sha256'):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, algorithm).digest()


def pack_release(release_dir):
    release_dir = Path(release_dir).resolve()
    release = json.loads((release_dir / 'release-manifest.json').read_text(encoding='utf-8'))
    asset = release['assets'][0]
    if Path(asset['filename']).name != asset['filename']:
        raise ValueError('发行文件名必须位于release目录')
    archive = release_dir / asset['filename']
    if digest(archive).hex() != asset['sha256']:
        raise ValueError('发行ZIP SHA-256不匹配')
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    if not npm:
        raise ValueError('npm is required to build the market package')
    with tempfile.TemporaryDirectory(prefix='wc-npm-') as directory:
        extract(archive, Path(directory))
        staging = Path(directory) / 'workflow-canvas-sidebar'
        validate_public_tree(staging)
        package = json.loads((staging / 'package.json').read_text(encoding='utf-8'))
        hooks = {'preinstall', 'install', 'postinstall', 'prepare'} & set(package.get('scripts', {}))
        if hooks:
            raise ValueError('市场包不能依赖安装钩子: ' + ', '.join(sorted(hooks)))
        for name in ('runtime/python/python.exe', 'runtime/python/python312.zip',
                     'server/canvas_editor_server.py', 'build-info.json',
                     'dependencies.json', 'asset-manifest.json'):
            if not (staging / name).is_file():
                raise ValueError('完整发行包缺少: ' + name)
        patch = package['dsh']['bundle']['patch']
        if not (staging / patch).is_file():
            raise ValueError('dsh.bundle.patch is missing from the release')
        asset_manifest = json.loads((staging / 'asset-manifest.json').read_text(encoding='utf-8'))
        for entry in asset_manifest['files']:
            if digest(staging / entry['path']).hex() != entry['sha256']:
                raise ValueError('发行文件SHA-256不匹配: ' + entry['path'])
        expected = {p.relative_to(staging).as_posix(): digest(p).hex()
                    for p in staging.rglob('*') if p.is_file()}
        unpacked_bytes = sum((staging / name).stat().st_size for name in expected)
        if unpacked_bytes > MAX_UNPACKED_BYTES:
            raise ValueError(f'市场包解压体积 {unpacked_bytes} 字节超过上限 {MAX_UNPACKED_BYTES} 字节')
        result = subprocess.run([npm, 'pack', '--ignore-scripts', '--json',
                                 '--pack-destination', str(release_dir)], cwd=staging,
                                capture_output=True, text=True, encoding='utf-8', check=True)
        filename = json.loads(result.stdout)[0]['filename']
        packed = release_dir / filename
        with tarfile.open(packed, 'r:gz') as tar:
            actual = {item.name.removeprefix('package/'): hashlib.file_digest(tar.extractfile(item), 'sha256').hexdigest()
                      for item in tar.getmembers() if item.isfile()}
        if actual != expected:
            names = sorted(name for name in expected.keys() | actual.keys()
                           if expected.get(name) != actual.get(name))
            raise ValueError('npm包没有逐字节保留完整发行文件: ' + ', '.join(names))
        manifest = {'name': package['name'], 'version': package['version'],
                    'filename': filename, 'build': release['build'],
                    'sha256': digest(packed).hex(), 'bytes': packed.stat().st_size,
                    'integrity': 'sha512-' + base64.b64encode(digest(packed, 'sha512')).decode('ascii'),
                    'files': len(actual), 'unpacked_bytes': unpacked_bytes,
                    'max_unpacked_bytes': MAX_UNPACKED_BYTES, 'install_scripts_required': False}
        write_json(release_dir / 'npm-manifest.json', manifest)
        sums = [digest(p).hex() + '  ' + p.name for p in sorted(release_dir.iterdir())
                if p.is_file() and p.name != 'SHA256SUMS.txt']
        (release_dir / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n', encoding='utf-8')
        return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'dist')
    args = parser.parse_args()
    print(json.dumps(pack_release(args.release_dir), ensure_ascii=False))
