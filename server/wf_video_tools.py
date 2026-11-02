"""Fixed local Remotion toolchain entry point for a workflow workspace.

Doctor performs read-only dependency resolution. Render is opt-in and keeps its
project, public assets, bundle, temporary JS and output within the workspace.
This is path validation, not an operating-system sandbox for arbitrary TSX code.
"""
import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import math
import struct
import wave
import signal
import stat
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def configured_tools():
    home = Path(os.environ.get('CANVAS_HOME') or Path(__file__).resolve().parent)
    try:
        value = json.loads((home / 'kb/execution-tools.json').read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


_TOOLS = configured_tools()
NODE = Path(os.environ.get('CANVAS_NODE') or _TOOLS.get('node') or shutil.which('node') or 'node.exe')
CHROME = Path(os.environ.get('CANVAS_CHROME') or _TOOLS.get('chrome') or shutil.which('chrome')
              or str(Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe'))
NODE_MODULES = Path(os.environ.get('CANVAS_NODE_MODULES') or _TOOLS.get('remotion_dependencies')
                    or str(Path(__file__).resolve().parents[1] / 'runtime/node/node_modules'))
PACKAGES = ("@remotion/bundler", "@remotion/renderer", "remotion", "react")

DOCTOR_JS = r"""
const fs = require('node:fs'); const path = require('node:path');
const root = process.argv[1]; const packages = {};
for (const name of ['@remotion/bundler','@remotion/renderer','remotion','react']) {
  try {
    const entry = require.resolve(path.join(root,name));
    const metadata = JSON.parse(fs.readFileSync(path.join(root,name,'package.json'),'utf8'));
    packages[name] = {available:true,version:metadata.version,entry};
  } catch { packages[name] = {available:false}; }
}
process.stdout.write(JSON.stringify({packages}));
"""

RENDER_JS = r"""
const fs = require('node:fs'); const path = require('node:path');
const cfg = JSON.parse(process.argv[2]);
const inside = (file,root) => {const rel = path.relative(root,file);return !rel.startsWith('..'+path.sep) && rel!=='..' && !path.isAbsolute(rel);};
const denyDownload = () => {throw new Error('Browser download is disabled; configure the installed Chrome executable.');};
(async () => {
  const {bundle} = require(path.join(cfg.nodeModules,'@remotion/bundler'));
  const {selectComposition,renderMedia} = require(path.join(cfg.nodeModules,'@remotion/renderer'));
  const publicDir = fs.existsSync(path.join(cfg.project,'public')) ? path.join(cfg.project,'public') : null;
  const guardPlugin = { apply(compiler) {
    compiler.hooks.normalModuleFactory.tap('WorkflowScope', factory => {
      factory.hooks.afterResolve.tap('WorkflowScope', data => {
        const resource = data && data.createData && data.createData.resource;
        if (!resource) return;
        const file = resource.split('?')[0];
        if (path.isAbsolute(file) && !inside(file,cfg.workspace) && !inside(file,cfg.nodeModules)) {
          throw new Error('Bundled input escapes the workflow workspace and fixed dependencies.');
        }
      });
    });
  }};
  const serveUrl = await bundle({entryPoint:cfg.entry,rootDir:cfg.project,publicDir,
    outDir:path.join(cfg.scratch,'bundle'),enableCaching: false,symlinkPublicDir:false,
    onSymlinkDetected: () => {throw new Error('Project asset links are not allowed.');},
    webpackOverride: c => ({...c,cache:false,plugins:[...(c.plugins||[]),guardPlugin],
      resolve:{...c.resolve,modules:[cfg.nodeModules,'node_modules'],
        alias:{...(c.resolve && c.resolve.alias || {}),react:path.join(cfg.nodeModules,'react'),'react-dom':path.join(cfg.nodeModules,'react-dom'),remotion:path.join(cfg.nodeModules,'remotion')}}})});
  const inputProps = {};
  const composition = await selectComposition({serveUrl,id:cfg.composition,inputProps,
    browserExecutable:cfg.browser,chromeMode:'chrome-for-testing',onBrowserDownload:denyDownload});
  await renderMedia({composition,serveUrl,codec:'h264',outputLocation:cfg.output,inputProps,
    browserExecutable:cfg.browser,chromeMode:'chrome-for-testing',onBrowserDownload:denyDownload,overwrite:false,
    concurrency:1,logLevel:'error'});
  process.stdout.write(JSON.stringify({ok:true,output:cfg.output,composition:cfg.composition}));
})().catch(error => {process.stderr.write(String(error.message||error), () => process.exit(1));});
"""


def _redact(value):
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", str(value))[:2000]


def _is_link(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _relative(value):
    if not isinstance(value, str) or not value or ntpath.splitdrive(value)[0]:
        raise ValueError("A workspace-relative path is required")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or value == ".":
        raise ValueError("Invalid workspace-relative path")
    for part in path.parts:
        if (part in {".", ".."} or part.endswith((".", " ")) or any(ord(c) < 32 for c in part)
                or any(c in part for c in '\\/:<>"|?*')
                or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I)):
            raise ValueError("Invalid workspace path component")
    return path.parts


def _within(root, relative):
    current = root
    if _is_link(root):
        raise ValueError("Workspace links/junctions are not allowed")
    for part in _relative(relative):
        if current.exists() and not current.is_dir():
            raise ValueError("Workspace path parent is not a directory")
        current = current / part
        if _is_link(current):
            raise ValueError("Workspace links/junctions are not allowed")
    try:
        inside = os.path.normcase(os.path.commonpath([str(root.resolve()), str(current.resolve())])) == os.path.normcase(str(root.resolve()))
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("Input or output escapes the workspace")
    return current


def _project_tree(project):
    for directory, dirs, files in os.walk(project, followlinks=False):
        for name in dirs + files:
            candidate = Path(directory) / name
            if _is_link(candidate):
                raise ValueError("Project links/junctions are not allowed")
            if candidate.is_file() and not stat.S_ISREG(candidate.lstat().st_mode):
                raise ValueError("Project inputs must be regular files")


def _environment(scratch=None):
    # Chrome needs the real Windows profile locations to distinguish its default
    # data directory from Remotion's new, separate --user-data-dir. Spoofing all
    # profile variables makes current Chrome reject the debugging connection.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATHEXT",
               "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME", "HOMEDRIVE", "HOMEPATH"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["PATH"] = str(NODE.parent) + os.pathsep + str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32")
    env["NODE_PATH"] = str(NODE_MODULES)
    if scratch is not None:
        for name, suffix in {"TEMP": "tmp", "TMP": "tmp"}.items():
            directory = scratch / suffix
            directory.mkdir(parents=True, exist_ok=True)
            env[name] = str(directory)
    return env


def _stop_tree(child):
    try:
        if os.name == "nt":
            cleanup = subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            confirmed = cleanup.returncode == 0
        else:
            os.killpg(child.pid, signal.SIGKILL)
            confirmed = True
        child.wait(timeout=5)
        return confirmed
    except (OSError, subprocess.TimeoutExpired):
        try:
            child.kill()
            child.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return False


def _run(command, *, cwd, env, timeout, tree=False, cancel_event=None):
    try:
        if tree:
            child = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
                                     start_new_session=os.name != "nt")
            finished = threading.Event()
            communication = {}
            def drain():
                try:
                    communication["value"] = child.communicate()
                except Exception as exc:
                    communication["error"] = exc
                finally:
                    finished.set()
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            cancelled = False
            while not finished.wait(0.1):
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if time.monotonic() >= deadline:
                    break
            timed_out = not finished.is_set()
            failed_read = communication.get("error")
            if isinstance(failed_read, subprocess.TimeoutExpired):
                timed_out = True
            if cancelled or timed_out:
                confirmed = _stop_tree(child)
                reader.join(timeout=3)
                reason = "cancelled; process-tree cleanup " if cancelled else "deadline exceeded; process-tree cleanup "
                raise ValueError("Local Remotion " + reason
                                 + ("confirmed" if confirmed else "NOT confirmed; stop residual processes before retry"))
            if failed_read is not None:
                raise ValueError("Local Remotion command failed: " + _redact(failed_read))
            stdout, stderr = communication.get("value", ("", ""))
            for stream in (getattr(child, "stdout", None), getattr(child, "stderr", None)):
                if stream is not None:
                    stream.close()
            return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
        return subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Local Remotion command failed: " + _redact(exc)) from exc


def doctor():
    """Read-only package resolution; never opens a browser or creates media."""
    problems = []
    packages = {}
    for label, path in (("Node", NODE), ("Chrome browser", CHROME)):
        if not path.is_file():
            problems.append(label + " is not installed at its configured path")
    if not NODE_MODULES.is_dir():
        problems.append("Fixed Remotion node_modules directory is missing")
    if NODE.is_file() and NODE_MODULES.is_dir():
        try:
            result = _run([str(NODE), "-e", DOCTOR_JS, str(NODE_MODULES)], cwd=NODE_MODULES.parent,
                          env=_environment(), timeout=15)
            if result.returncode:
                problems.append("Cannot resolve installed Remotion packages")
            else:
                packages = json.loads(result.stdout).get("packages", {})
        except (ValueError, TypeError):
            problems.append("Cannot resolve installed Remotion packages")
    for package in PACKAGES:
        if not packages.get(package, {}).get("available"):
            problems.append("Missing installed package: " + package)
    remotion_versions = {packages.get(name, {}).get("version") for name in PACKAGES[:3] if packages.get(name, {}).get("available")}
    if len(remotion_versions) > 1:
        problems.append("Installed Remotion package versions do not match")
    return {"available": not problems, "node": str(NODE), "browser": str(CHROME),
            "node_modules": str(NODE_MODULES), "packages": packages, "problems": problems,
            "browser_download": "disabled", "media_generated": False}


def render(*, workspace, project, entry, composition, output, scratch="scratch/remotion", timeout=1200,
           cancel_event=None):
    """Run the explicitly requested render with existing tools; do not install anything."""
    root = Path(workspace).absolute()
    if not root.is_dir() or _is_link(root):
        raise ValueError("Workspace must be an existing directory without links")
    project_path = _within(root, project)
    if not project_path.is_dir():
        raise ValueError("Remotion project directory does not exist")
    _project_tree(project_path)
    entry_path = _within(project_path, entry)
    if not entry_path.is_file() or entry_path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"}:
        raise ValueError("A project TypeScript/JavaScript entry file is required")
    output_path = _within(root, output)
    if output_path.suffix.lower() != ".mp4" or output_path.exists():
        raise ValueError("Output must be a new MP4 file inside the workspace")
    scratch_parts = _relative(scratch)
    if len(scratch_parts) < 2 or scratch_parts[0] != "scratch" or not scratch_parts[1].startswith("remotion"):
        raise ValueError("Scratch must be under workspace scratch/remotion...")
    scratch_path = _within(root, scratch)
    if not isinstance(composition, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", composition):
        raise ValueError("Invalid composition ID")
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 1800:
        raise ValueError("Render timeout must be 1..1800 seconds")
    health = doctor()
    if not health["available"]:
        raise ValueError("Local rendering tools unavailable: " + "; ".join(health["problems"]))
    invocation = _within(root, scratch + "/invoke-" + uuid.uuid4().hex)
    invocation.mkdir(parents=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    script = invocation / "render.cjs"
    script.write_text(RENDER_JS, encoding="utf-8")
    config = {"workspace": str(root), "project": str(project_path), "entry": str(entry_path),
              "composition": composition, "output": str(output_path), "scratch": str(invocation),
              "nodeModules": str(NODE_MODULES), "browser": str(CHROME)}
    # Chrome's internal cache paths still hit Windows path limits when its
    # profile sits below a deeply nested project workspace. Keep only browser
    # runtime files in a new short temporary directory; project/bundle/output
    # paths remain scoped to the workspace.
    with tempfile.TemporaryDirectory(prefix='wc-render-') as browser_temp:
        browser_root = Path(browser_temp).resolve()
        if browser_root.parent != Path(tempfile.gettempdir()).resolve() or not browser_root.name.startswith('wc-render-'):
            raise ValueError('Unexpected browser temporary directory')
        result = _run([str(NODE), str(script), json.dumps(config, ensure_ascii=False)],
                      cwd=invocation, env=_environment(browser_root), timeout=timeout, tree=True,
                      cancel_event=cancel_event)
    if result.returncode:
        raise ValueError("Remotion render failed: " + _redact(result.stderr or "No diagnostic output"))
    output_path = _within(root, output)
    if _is_link(output_path) or not output_path.is_file() or output_path.stat().st_size == 0:
        raise ValueError("Remotion completed without a valid output artifact")
    with output_path.open("rb") as artifact:
        digest = hashlib.file_digest(artifact, "sha256").hexdigest()
    return {"ok": True, "output": output, "composition": composition,
            "bytes": output_path.stat().st_size, "sha256": digest,
            "scratch": str(invocation.relative_to(root)).replace("\\", "/")}


def _broker_config():
    url = os.environ.get("CANVAS_RENDER_BROKER_URL", "").strip()
    token = os.environ.get("CANVAS_RENDER_BROKER_TICKET", "").strip()
    if not url and not token:
        return None
    if not url or not token or len(token) > 500:
        raise ValueError("Incomplete render broker configuration")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ValueError("Render broker must be a loopback HTTP endpoint")
    return url.rstrip("/"), token


def _broker_render(cfg, *, project, entry, composition, output, scratch="scratch/remotion", timeout=1200):
    payload = {"project": project, "entry": entry, "composition": composition,
               "output": output, "scratch": scratch, "timeout": timeout}
    return _broker_request(cfg, '/render', payload, timeout)


def _broker_request(cfg, path, payload, timeout=1200):
    url, token = cfg
    request = urllib.request.Request(
        url + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=min(1830, max(30, float(timeout) + 30))) as response:
            result = json.loads(response.read(4 * 1024 * 1024 + 1).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(64 * 1024).decode("utf-8")).get("error")
        except (OSError, UnicodeError, ValueError):
            error = None
        raise ValueError("Render broker rejected the request: " + _redact(error or ("HTTP " + str(exc.code)))) from exc
    except (urllib.error.URLError, OSError, UnicodeError, ValueError) as exc:
        raise ValueError("Render broker unavailable: " + _redact(exc)) from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError("Render broker returned an invalid result")
    return result


def tone(*, workspace, output, duration=30, frequency=440, volume=0.035):
    if (type(duration) not in (int, float) or not 0.1 <= duration <= 600 or
            type(frequency) not in (int, float) or not 40 <= frequency <= 2000 or
            type(volume) not in (int, float) or not 0 <= volume <= 0.2):
        raise ValueError('Tone duration/frequency/volume is outside the allowed range')
    root = Path(workspace).resolve()
    path = _within(root, output)
    if path.suffix != '.wav' or path.exists():
        raise ValueError('Tone output must be a new workspace WAV file')
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 22050
    with wave.open(str(path), 'wb') as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(rate)
        for second in range(math.ceil(duration)):
            data = bytearray()
            for sample in range(second * rate, min(int(duration * rate), (second + 1) * rate)):
                t = sample / rate
                envelope = min(1, t * 3, (duration - t) * 3)
                pulse = max(0, 1 - (t % 10) / 1.5)
                value = volume * envelope * pulse * math.sin(2 * math.pi * frequency * t)
                data.extend(struct.pack('<h', round(32767 * value)))
            target.writeframes(data)
    return {'ok': True, 'output': output, 'duration': duration, 'frequency': frequency,
            'volume': volume, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    health = sub.add_parser("doctor")
    health.add_argument("--json", action="store_true")
    job = sub.add_parser("render")
    for name in ("workspace", "project", "entry", "composition", "output"):
        job.add_argument("--" + name, required=True)
    job.add_argument("--scratch", default="scratch/remotion")
    job.add_argument("--timeout", type=int, default=1200)
    sound = sub.add_parser('tone')
    sound.add_argument('--workspace', required=True)
    sound.add_argument('--output', required=True)
    sound.add_argument('--duration', type=float, default=30)
    sound.add_argument('--frequency', type=float, default=440)
    sound.add_argument('--volume', type=float, default=0.035)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == 'tone':
            payload = {name: getattr(args, name) for name in ('output', 'duration', 'frequency', 'volume')}
            broker = _broker_config()
            result = _broker_request(broker, '/tone', payload, 30) if broker else tone(workspace=args.workspace, **payload)
        else:
            broker = _broker_config()
            if broker is not None:
                result = _broker_render(broker, **{name: getattr(args, name) for name in (
                    "project", "entry", "composition", "output", "scratch", "timeout")})
            else:
                result = render(**{name: getattr(args, name) for name in (
                    "workspace", "project", "entry", "composition", "output", "scratch", "timeout")})
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get("available", result.get("ok", False)) else 1
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": _redact(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
