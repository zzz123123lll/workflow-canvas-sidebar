"""Assemble the Windows runtime from hash-locked official archives, without pip resolution."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def obtain(item, cache, offline=False):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / item['filename']
    if not target.exists():
        if offline:
            raise ValueError('离线缓存缺少：' + item['filename'])
        request = urllib.request.Request(item['url'], headers={'User-Agent': 'workflow-canvas-release/0.17'})
        temporary = target.with_suffix(target.suffix + '.partial')
        with urllib.request.urlopen(request, timeout=120) as source, temporary.open('wb') as out:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                out.write(chunk)
        temporary.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != item['sha256']:
        raise ValueError('下载缓存SHA-256不匹配：' + item['filename'])
    return target


def extract(archive, destination):
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            rel = PurePosixPath(entry.filename)
            if rel.is_absolute() or '..' in rel.parts or '\\' in entry.filename or ':' in entry.filename:
                raise ValueError('依赖压缩包存在越界路径')
            if entry.is_dir():
                continue
            target = destination.joinpath(*rel.parts)
            for parent in [target, *target.parents]:
                if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
                    raise ValueError('依赖输出目录不能经过链接')
                if parent == destination:
                    break
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as inp, target.open('wb') as out:
                for chunk in iter(lambda: inp.read(1024 * 1024), b''):
                    out.write(chunk)


def assemble(destination, cache, offline=False):
    lock = json.loads((ROOT / 'dependencies.lock.json').read_text(encoding='utf-8'))
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('运行时构建目标须为空目录；请选择新的staging目录')
    destination.mkdir(parents=True, exist_ok=True)
    extract(obtain(lock['python'], cache, offline), destination)
    for wheel in lock['wheels']:
        extract(obtain(wheel, cache, offline), destination / 'Lib/site-packages')
    # The embedded Python is deliberately plugin-local and does not use a global installation.
    (destination / 'python312._pth').write_text('python312.zip\n.\nLib\\site-packages\n..\\..\\server\nimport site\n', encoding='utf-8', newline='\n')
    return lock


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--cache', type=Path, default=ROOT / '.cache/dependencies')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    lock = assemble(args.destination, args.cache, args.offline)
    print('Runtime assembled from CPython ' + lock['python']['version'] + ' and ' + str(len(lock['wheels'])) + ' locked wheels.')
