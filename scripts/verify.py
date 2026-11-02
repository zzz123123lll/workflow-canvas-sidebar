"""One account-free verification entry point. All test data stays in a temporary home."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='workflow-canvas-tests-') as directory:
        env = dict(os.environ, CANVAS_HOME=directory, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=str(ROOT / 'server') + os.pathsep + os.environ.get('PYTHONPATH', ''))
        print('Python:', sys.executable, flush=True)
        print('Shipped source:', ROOT / 'server', flush=True)
        print('Isolated test data:', directory, flush=True)
        commands = [[sys.executable, '-m', 'pytest', 'tests', '-q'], ['node', '--check', 'index.js'],
                    ['node', '--check', 'client.js'], ['node', '--test', *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'tests').glob('*.test.mjs'))]],
                    ['node', 'tests/test_canvas_client.cjs'], ['node', 'tests/test_canvas_product.cjs'],
                    ['node', 'tests/test_document_preview.cjs']]
        for command in commands:
            print('Running:', ' '.join(command), flush=True)
            result = subprocess.run(command, cwd=ROOT, env=env)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
