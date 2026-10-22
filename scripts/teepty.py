#!/usr/bin/env python3
"""teepty — run a command in a pseudo-terminal, mirroring output to a log file.

Why this exists: a windowed dev server must (a) see a real TTY so colours,
progress bars and interactive keys work, and (b) leave a log an agent can read
while it is *still running*.

The obvious solutions both fail:

  * plain redirection (`cmd > log`) — stdout is no longer a TTY, so the child
    disables colours and interactive behaviour;
  * macOS `script(1)` — allocates the right pty, but on BSD it buffers and only
    writes the transcript when the process exits, so the log is empty for as long
    as the server is up. That is precisely when you want to read it.

So: allocate the pty ourselves, copy master -> terminal and master -> log on every
read, and forward stdin so interactive keys still reach the child.

    teepty <logfile> <command> [args...]

Exit status is the child's. Falls back to plain execution if a pty cannot be
allocated, because losing logging must never stop the service from starting.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _current_winsize() -> tuple[int, int] | None:
    try:
        data = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols = struct.unpack("HHHH", data)[:2]
        return (rows, cols) if rows and cols else None
    except OSError:
        return None


def run(log_path: str, cmd: list[str]) -> int:
    master, slave = pty.openpty()
    size = _current_winsize()
    if size:
        _set_winsize(slave, *size)

    pid = os.fork()
    if pid == 0:
        # Child: become a session leader and take the slave as controlling tty,
        # so job control and key handling behave as they would in a real terminal.
        try:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0)
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(master)
            os.close(slave)
            os.execvp(cmd[0], cmd)
        except Exception:
            os._exit(127)

    os.close(slave)

    # Keep the child's terminal the same size when the window is resized.
    def on_winch(_signum, _frame):
        new = _current_winsize()
        if new:
            _set_winsize(master, *new)

    try:
        signal.signal(signal.SIGWINCH, on_winch)
    except (ValueError, OSError):
        pass

    log = None
    try:
        log = open(log_path, "ab", buffering=0)
    except OSError:
        log = None

    stdin_fd = sys.stdin.fileno()
    watch_stdin = True
    try:
        while True:
            watch = [master] + ([stdin_fd] if watch_stdin else [])
            try:
                readable, _, _ = select.select(watch, [], [], 1.0)
            except InterruptedError:
                continue
            except OSError:
                break

            if master in readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                try:
                    os.write(1, chunk)
                except OSError:
                    pass
                if log is not None:
                    try:
                        log.write(chunk)
                    except OSError:
                        pass
            if watch_stdin and stdin_fd in readable:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    data = b""
                if not data:
                    # EOF on our stdin: stop watching it, but keep the child alive.
                    watch_stdin = False
                else:
                    try:
                        os.write(master, data)
                    except OSError:
                        watch_stdin = False
    finally:
        if log is not None:
            try:
                log.close()
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass

    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write("usage: teepty <logfile> <command> [args...]\n")
        return 2
    log_path, cmd = argv[1], argv[2:]
    try:
        return run(log_path, cmd)
    except OSError as exc:
        # No pty available (rare, but possible in restricted environments): run the
        # command plainly rather than refusing to start it.
        sys.stderr.write(f"teepty: pty unavailable ({exc}); running without logging\n")
        try:
            pid = os.fork()
            if pid == 0:
                os.execvp(cmd[0], cmd)
            _, status = os.waitpid(pid, 0)
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        except OSError:
            return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv))
