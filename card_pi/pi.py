"""Pi executable discovery, installation, sessions and JSONL transport."""
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
import hashlib
import json
import os
from pathlib import Path
import platform
import re
from queue import SimpleQueue
import shutil
import signal
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
from urllib.request import urlopen
from uuid import uuid4

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "card-pi"
DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "card-pi"
AGENT = Path(os.environ.get("PI_CODING_AGENT_DIR") or HOME / ".pi/agent")
VERSION = "1.6.0"
RELEASE = f"https://github.com/earendil-works/pi/releases/download/v{VERSION}/pi-linux-arm64.tar.gz"
SHA256 = "5404547b9739be596d50ae7142c52535899e8e7dd37a7bace9be593e0355b468"
MANAGED = DATA / f"pi-{VERSION}"
INSTALLER = "https://pi.dev/install.sh"


def validate_pi(path):
    path = Path(path).expanduser().absolute()
    env = dict(os.environ, PATH=f"{path.parent}:{HOME / '.local/bin'}:{os.environ.get('PATH', os.defpath)}")
    result = subprocess.run([str(path), "--version"], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=8)
    version = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if result.returncode or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][\w.+-]+)?", version):
        raise RuntimeError((result.stderr.strip() or "pi --version failed")[-230:])
    return str(path), version, env


def detect(custom=""):
    candidates = [custom, shutil.which("pi"), HOME / ".local/bin/pi",
                  AGENT / "bin/pi", HOME / ".pi/bin/pi", MANAGED / "pi"]
    errors = []
    for path in dict.fromkeys(p for p in candidates if p):
        if Path(path).expanduser().is_file():
            try:
                return validate_pi(path), ""
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
    return None, errors[0] if errors else ""


@contextmanager
def install_lock():
    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "install.lock").open("a") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个 Card Pi 正在安装") from exc
        yield


def installer_environment():
    env = dict(os.environ)
    env["PATH"] = f"{HOME / '.local/bin'}:{AGENT / 'bin'}:{env.get('PATH', os.defpath)}"
    for name in ("PI_INSTALLER_API_BASE", "PI_MANAGED_INSTALL_ROOT", "PI_MANAGED_INSTALL"):
        env.pop(name, None)
    return env


def node_available(env):
    try:
        node = subprocess.run(
            ["node", "-e", "const [major, minor] = process.versions.node.split('.').map(Number); "
             "process.exit(major > 22 || (major === 22 && minor >= 19) ? 0 : 1)"],
            env=env, capture_output=True, timeout=5,
        )
        npm = subprocess.run(["npm", "--version"], env=env, capture_output=True, timeout=5)
        return node.returncode == npm.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def official_install(env, progress, cancel):
    progress("正在运行 Pi 官方安装器…")
    with urlopen(INSTALLER, timeout=20) as response:
        script = response.read(512 * 1024 + 1)
        if response.geturl() != INSTALLER or len(script) > 512 * 1024 or not script.startswith(b"#!/bin/sh"):
            raise RuntimeError("无法验证 Pi 官方安装脚本地址或格式")
    if cancel.is_set():
        raise RuntimeError("已取消安装")
    installer_env = dict(env, TERM="dumb", npm_config_fetch_retries="1",
                         npm_config_fetch_timeout="30000")
    with TemporaryDirectory(prefix="installer-", dir=DATA) as directory:
        path = Path(directory) / "install.sh"
        path.write_bytes(script)
        with (Path(directory) / "output").open("w+b") as log:
            process = subprocess.Popen(["/bin/sh", str(path)], cwd=HOME, env=installer_env,
                                       start_new_session=True, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
            deadline = monotonic() + 240
            while process.poll() is None:
                if cancel.wait(0.15) or monotonic() >= deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    if cancel.is_set():
                        raise RuntimeError("已取消安装")
                    raise RuntimeError("Pi 官方安装器超时，请检查网络")
            if process.returncode:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 4096))
                lines = [line.strip() for line in log.read().decode("utf-8", "replace").splitlines()
                         if line.strip()]
                raise RuntimeError("Pi 官方安装失败：" + (" / ".join(lines[-3:]) or str(process.returncode)))
    core, error = detect()
    if not core:
        raise RuntimeError(error or "安装完成后找不到 Pi，请指定可执行文件路径")
    return core


def install(progress, cancel=None):
    """Install from official Pi distribution channels under the current account."""
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        raise RuntimeError("Pi 安装需要 ARM64 Linux")
    if os.geteuid() == 0:
        raise RuntimeError("请以普通用户安装 Pi")
    if cancel is None:
        cancel = Event()
    with install_lock():
        core, _ = detect()
        if core:
            return core
        env = installer_environment()
        if node_available(env) and not shutil.which("pi", path=env["PATH"]):
            return official_install(env, progress, cancel)
        return standalone_install(progress, cancel)


def standalone_install(progress, cancel):
    if shutil.disk_usage(DATA).free < 220 * 1024**2:
        raise RuntimeError("请先腾出至少 220 MiB 磁盘空间")
    if MANAGED.exists():
        try:
            return validate_pi(MANAGED / "pi")
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            # Preserve the previous files until the replacement passes validation.
            pass
    with TemporaryDirectory(prefix="install-", dir=DATA) as tmp:
        folder = Path(tmp)
        archive = folder / "pi.tar.gz"
        digest = hashlib.sha256()
        progress("正在下载 Pi…")
        with urlopen(RELEASE, timeout=20) as response, archive.open("wb") as output:
            if not response.geturl().startswith("https://"):
                raise RuntimeError("Pi 下载需要 HTTPS")
            size = 0
            megabytes = 0
            while chunk := response.read(256 * 1024):
                if cancel.is_set():
                    raise RuntimeError("已取消安装")
                size += len(chunk)
                if size > 75 * 1024**2:
                    raise RuntimeError("Pi 下载文件过大")
                digest.update(chunk)
                output.write(chunk)
                if size // 1024**2 > megabytes:
                    megabytes = size // 1024**2
                    progress(f"正在下载 Pi… {megabytes} MiB")
        if digest.hexdigest() != SHA256:
            raise RuntimeError("Pi 发布包校验失败")
        progress("正在解压和验证 Pi…")
        unpacked = folder / "payload"
        unpacked.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter="data")
        binaries = [path for path in unpacked.rglob("pi") if path.is_file()
                    and os.access(path, os.X_OK)]
        if not binaries:
            raise RuntimeError("发布包中找不到 Pi")
        binary = min(binaries, key=lambda path: len(path.parts))
        if validate_pi(binary)[1] != VERSION:
            raise RuntimeError("Pi 发布包版本与预期不一致")
        if cancel.is_set():
            raise RuntimeError("已取消安装")
        backup = folder / "previous"
        if MANAGED.exists():
            MANAGED.rename(backup)
        try:
            binary.parent.rename(MANAGED)
            return validate_pi(MANAGED / "pi")
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            if MANAGED.exists():
                MANAGED.rename(folder / "failed")
            if backup.exists():
                backup.rename(MANAGED)
            raise


def load_settings():
    try:
        value = json.loads((CONFIG / "settings.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(value):
    CONFIG.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=CONFIG) as directory:
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps(value, ensure_ascii=False))
        path.replace(CONFIG / path.name)


def session_dirs(cwd):
    # Pi's default project directory encoding, with CLI/env/settings overrides.
    def configured(path):
        try:
            return json.loads(path.read_text()).get("sessionDir")
        except (OSError, ValueError, AttributeError):
            return None

    project = configured(cwd / ".pi/settings.json")
    global_dir = configured(AGENT / "settings.json")
    override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if override or project or global_dir:
        for value in (override, project, global_dir):
            if isinstance(value, str) and value:
                path = Path(value).expanduser()
                yield path if path.is_absolute() else cwd / path
    encoded = "--" + str(cwd).removeprefix("/").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    yield AGENT / "sessions" / encoded


TRASH = ".card-pi-trash"


def session_list(cwd, extra=None, *, trashed=False):
    rows = []
    folders = [*session_dirs(cwd)]
    if extra:
        folders.append(Path(extra).parent)
    seen = set()
    for folder in folders:
        source_dir = folder / TRASH if trashed else folder
        for path in source_dir.glob("*.jsonl"):
            if path in seen or path.is_symlink():
                continue
            seen.add(path)
            try:
                with path.open(encoding="utf-8") as source:
                    header = json.loads(source.readline())
                    if header.get("type") != "session" or Path(header.get("cwd", "")).resolve() != cwd:
                        continue
                    title = ""
                    first = ""
                    for line in source:
                        try:
                            entry = json.loads(line)
                            if entry.get("type") == "session_info":
                                title = entry.get("name") or title
                            if not first and entry.get("type") == "message" and entry["message"].get("role") == "user":
                                first = message_text(entry["message"])
                        except (ValueError, AttributeError, KeyError, TypeError):
                            continue
                rows.append((path.stat().st_mtime, " ".join((title or first or path.stem).split()), path))
            except (OSError, ValueError, AttributeError, TypeError, RuntimeError):
                continue
    return sorted(rows, reverse=True)


def trash_session(path):
    folder = path.parent / TRASH
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink():
        raise OSError("Invalid session trash directory")
    path.rename(folder / f"{uuid4().hex}--{path.name}")


def purge_session(path):
    prefix, separator, filename = path.name.partition("--")
    if (path.parent.name != TRASH or path.parent.is_symlink()
            or not separator or len(prefix) != 32
            or any(char not in "0123456789abcdef" for char in prefix)
            or not filename.endswith(".jsonl") or path.is_symlink()):
        raise ValueError("Invalid session trash entry")
    path.unlink()


def restore_session(path):
    _, separator, filename = path.name.partition("--")
    if path.parent.name != TRASH or not separator or filename in {"", ".", ".."}:
        raise ValueError("Invalid session trash entry")
    target = path.parent.parent / filename
    os.link(path, target)
    path.unlink()
    return target


def message_text(message):
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(item.get("text", "") for item in content
                   if isinstance(item, dict) and item.get("type") == "text")


class RPC:
    def __init__(self, core, cwd, session=None):
        path, _, env = core
        args = [path, "--mode", "rpc"]
        if session:
            args.extend(["--session", str(session)])
        self.events = SimpleQueue()
        self.process = subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1)
        self.out_thread = Thread(target=self.read, args=(self.process.stdout, True), daemon=True)
        self.err_thread = Thread(target=self.read, args=(self.process.stderr, False), daemon=True)
        self.out_thread.start()
        self.err_thread.start()

    def read(self, stream, protocol):
        for line in stream:
            if protocol:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.events.put(event)
            elif line.strip():
                self.events.put({"type": "stderr", "message": line.strip()[-320:]})
        if protocol:
            self.events.put({"type": "closed"})

    def send(self, command, **fields):
        return self.write({"type": command, "id": uuid4().hex, **fields})

    def write(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        return message["id"]

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            except ProcessLookupError:
                self.process.wait()
        for worker in (self.out_thread, self.err_thread):
            worker.join(timeout=0.3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
