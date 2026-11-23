"""An embedded PTY for Pi's original interactive commands."""
from collections import deque
from errno import EIO
from fcntl import ioctl
import json
import os
from pathlib import Path
import pty
import signal
from struct import pack
import subprocess
import sys
from tempfile import TemporaryDirectory
from termios import ICANON, TIOCSWINSZ, tcgetattr
from time import monotonic



if __name__ == "__main__":
    os.login_tty(sys.stdin.fileno())
    os.execv(sys.argv[1], sys.argv[1:])

from .device_screen import BACKSPACE, DOWN, ENTER, ESC, LEFT, RIGHT, TAB, UP, TEXT_KEYS

TERMINAL_KEYS = {
    BACKSPACE: b"\x7f", ENTER: b"\r", ESC: b"\x1b", TAB: b"\t",
    UP: b"\x1b[A", DOWN: b"\x1b[B", LEFT: b"\x1b[D", RIGHT: b"\x1b[C",
    102: b"\x1b[H", 104: b"\x1b[5~", 107: b"\x1b[F", 109: b"\x1b[6~",
    110: b"\x1b[2~", 111: b"\x1b[3~",
}


def terminal_key(key):
    if key.control:
        base = TEXT_KEYS.get(key.code, "")
        if base and ("a" <= base <= "z" or base in "[\\]"):
            return bytes([ord(base.upper()) & 31])
        if base == " ":
            return b"\x00"
    value = TERMINAL_KEYS.get(key.code, key.text.encode("utf-8"))
    return b"\x1b" + value if value and key.alt else value


class TerminalProcess:
    def __init__(self, args, cwd, env, *, cols, rows):
        import pyte

        self.screen = pyte.HistoryScreen(cols, rows, history=250)
        self.stream = pyte.ByteStream(self.screen)
        env = dict(env, TERM="xterm-256color", COLORTERM="truecolor")
        # The child acquires the controlling terminal before it execs Pi.
        master, slave = pty.openpty()
        try:
            ioctl(slave, TIOCSWINSZ, pack("HHHH", rows, cols, 0, 0))
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__)), *args], cwd=cwd, env=env,
                stdin=slave, stdout=slave, stderr=slave,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master = master
        os.set_blocking(master, False)

    def update(self):
        changed = False
        for _ in range(16):
            try:
                output = os.read(self.master, 8192)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == EIO:
                    break
                raise
            if not output:
                break
            self.stream.feed(output)
            changed = True
        return changed

    def key(self, key):
        value = terminal_key(key)
        if value:
            try:
                os.write(self.master, value)
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno != EIO:
                    raise

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    self.process.kill()
                self.process.wait()
        os.close(self.master)


class NativePi(TerminalProcess):
    def __init__(self, core, cwd, session, *, command=None, cols=44, rows=10):
        path, _, env = core
        self.pending_input = deque(maxlen=4096)
        self.last_edit = 0.0
        self.initial_slash = True
        self.command = command
        self.submit_command = None
        self.ready_at = None
        self.directory = TemporaryDirectory(prefix="card-pi-terminal-")
        self.report = Path(self.directory.name) / "session.json"
        env = dict(env, CARD_PI_SESSION_REPORT=str(self.report))
        args = [path, "--extension", str(Path(__file__).with_name("native-session.mjs"))]
        if session is not None and session.is_file():
            args.extend(["--session", str(session)])
        try:
            super().__init__(args, cwd, env, cols=cols, rows=rows)
        except BaseException:
            self.directory.cleanup()
            raise

    def update(self):
        changed = super().update()
        if self.initial_slash and self.report.exists():
            # session_start can fire before Pi has enabled raw input.
            try:
                raw = not tcgetattr(self.master)[3] & ICANON
            except OSError:
                raw = False
            if raw:
                if self.ready_at is None:
                    self.ready_at = monotonic() + 0.2
                elif monotonic() >= self.ready_at:
                    self.initial_slash = False
                    text = self.command or "/"
                    self.pending_input.extendleft(bytes([char]) for char in reversed(text.encode("ascii")))
                    self.submit_command = self.command
        if (self.submit_command and not self.pending_input and monotonic() - self.last_edit >= 0.18
                and any(line.strip() == self.submit_command for line in self.screen.display)):
            self.pending_input.append(b"\r")
            self.submit_command = None
        if self.pending_input:
            value = self.pending_input[0]
            now = monotonic()
            # Pi's slash picker updates asynchronously after text changes. Let it
            # settle before forwarding Enter, even if the keyboard batched events.
            if value != b"\r" or now - self.last_edit >= 0.18:
                try:
                    sent = os.write(self.master, value)
                    if sent == len(value):
                        self.pending_input.popleft()
                    else:
                        self.pending_input[0] = value[sent:]
                    if value == b"\x7f" or value[:1] >= b" ":
                        self.last_edit = now
                except BlockingIOError:
                    pass
                except OSError as exc:
                    if exc.errno != EIO:
                        raise
        return changed

    def key(self, key):
        value = terminal_key(key)
        if value:
            # Inputs at an initial trust prompt must not discard the selected page.
            if not self.command or self.report.exists():
                self.initial_slash = False
                self.submit_command = None
            if len(self.pending_input) < self.pending_input.maxlen:
                self.pending_input.append(value)

    def close(self):
        super().close()
        try:
            return json.loads(self.report.read_text())
        except (OSError, ValueError):
            return None
        finally:
            self.directory.cleanup()
