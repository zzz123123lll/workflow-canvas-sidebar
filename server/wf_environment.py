"""Local capability discovery and explicit tool configuration; no automatic downloads."""
import json
import os
from pathlib import Path
import shutil
import sys

TOOLS = {'python', 'node', 'chrome', 'ffmpeg', 'ffprobe', 'remotion_dependencies'}


def inspect(base):
    config_path = Path(base) / 'kb/execution-tools.json'
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        config = {}
    defaults = {'python': sys.executable, 'node': shutil.which('node') or '',
                'ffmpeg': shutil.which('ffmpeg') or '', 'ffprobe': shutil.which('ffprobe') or '',
                'chrome': str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe')}
    paths = {k: str(config.get(k) or defaults.get(k) or '') for k in sorted(TOOLS)}
    available = {k: bool(v and (Path(v).is_dir() if k == 'remotion_dependencies' else Path(v).is_file())) for k, v in paths.items()}
    if available['remotion_dependencies']:
        available['remotion_dependencies'] = all((Path(paths['remotion_dependencies']) / name / 'package.json').is_file()
            for name in ['remotion', '@remotion/bundler', '@remotion/renderer', 'react'])
    video = all(available.get(k) for k in ['node', 'chrome', 'ffmpeg', 'ffprobe', 'remotion_dependencies'])
    return {'ok': True, 'platform': sys.platform, 'paths': paths, 'available': available,
            'capabilities': {'documents': '内置可用', 'video': '配置后可用' if video else '待配置'},
            'video_ready': video, 'tested_host': ['0.2.1-rc.5'],
            'note': '账号模型通过Harness运行；视频工具由使用者安装配置。Remotion许可按其官方条款。'}


def configure(base, body):
    values = body.get('paths')
    if not isinstance(values, dict) or set(values) - TOOLS:
        raise ValueError('请填写支持的工具路径')
    current = inspect(base)['paths']
    for name, value in values.items():
        if not isinstance(value, str) or len(value) > 32700:
            raise ValueError('工具路径无效：' + name)
        value = value.strip().strip('"')
        if value:
            path = Path(value)
            if not path.is_absolute() or not (path.is_dir() if name == 'remotion_dependencies' else path.is_file()):
                raise ValueError('请先安装对应工具，再填写真实绝对路径：' + name)
            if name != 'remotion_dependencies' and path.name.lower() not in {
                    'python.exe', 'python', 'python3', 'node.exe', 'node', 'chrome.exe', 'chrome',
                    'chromium', 'ffmpeg.exe', 'ffmpeg', 'ffprobe.exe', 'ffprobe'}:
                raise ValueError('工具文件名不符合此配置用途：' + name)
            value = str(path.resolve())
        current[name] = value
    current['remotion_helper'] = str(Path(__file__).resolve().parent / 'wf_video_tools.py')
    target = Path(base) / 'kb/execution-tools.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, target)
    return inspect(base)
