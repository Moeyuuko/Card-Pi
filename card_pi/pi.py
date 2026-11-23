"""Pi executable discovery, installation, sessions and JSONL transport."""
import json
import os
from pathlib import Path
import re
from queue import SimpleQueue
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

HOME = Path.home()
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "card-pi"
AGENT = Path(os.environ.get("PI_CODING_AGENT_DIR") or HOME / ".pi/agent")
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
                  AGENT / "bin/pi", HOME / ".pi/bin/pi"]
    errors = []
    for path in dict.fromkeys(p for p in candidates if p):
        if Path(path).expanduser().is_file():
            try:
                return validate_pi(path), ""
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
    return None, errors[0] if errors else ""


def installer_command():
    env = dict(os.environ)
    env["PATH"] = f"{HOME / '.local/bin'}:{AGENT / 'bin'}:{env.get('PATH', os.defpath)}"
    for name in ("PI_INSTALLER_API_BASE", "PI_MANAGED_INSTALL_ROOT", "PI_MANAGED_INSTALL"):
        env.pop(name, None)
    # Download completely before executing. The script inherits the controlling PTY.
    script = r"""
set -e
file=$(mktemp)
trap 'rm -f -- "$file"' EXIT
curl --fail --silent --show-error --location --proto '=https' \
    --connect-timeout 20 --output "$file" "$1"
/bin/sh "$file"
"""
    return ["/bin/sh", "-c", script, "card-pi-installer", INSTALLER], env


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
