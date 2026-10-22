#!/usr/bin/env python3
"""Platform differences, isolated in one place.

Everything that is genuinely different between macOS/Linux and Windows lives here so
the orchestration logic stays readable and testable:

  * how a shell command is invoked (POSIX login shell vs `cmd /c`)
  * how to find which process holds a TCP port (`lsof` vs `netstat -ano`)
  * how to tell whether a pid is alive without `/proc`
  * how to kill a process *tree* (process groups vs `taskkill /T`)
  * how to resolve an executable name (PATH vs PATH+PATHEXT)

Windows support is implemented against Windows 11 + Windows Terminal. The logic is
covered by CI on windows-latest; whether a terminal window visually appears cannot
be tested there, because hosted runners have no interactive desktop. That claim is
deliberately not made.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# CreateProcess flags used on Windows.
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


def shell_argv(command: str, shell: str = "auto") -> list[str]:
    """Wrap a user command so it runs the way the platform expects.

    POSIX uses a *login* shell (`-l`) on purpose: nvm, pyenv and conda only exist in
    the profile, and a service started without it gets the wrong interpreter.
    Windows has no equivalent, so `cmd /c` is used, with PowerShell as an opt-in via
    `shell: powershell` in the config.
    """
    if not IS_WINDOWS:
        return ["/bin/sh", "-lc", command]
    name = (shell or "auto").lower()
    if name in ("powershell", "pwsh", "ps"):
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return [comspec, "/d", "/s", "/c", command]


def stop_command_argv(command: str, shell: str = "auto") -> list[str]:
    return shell_argv(command, shell)


def runner_suffix() -> str:
    """Windows cannot execute a shebang script directly; a `.cmd` shim is used instead."""
    return ".cmd" if IS_WINDOWS else ".command"


def login_shell() -> str | None:
    """POSIX login shell, or None on Windows (there is no equivalent)."""
    if IS_WINDOWS:
        return None
    shell = os.environ.get("SHELL") or ""
    if shell and Path(shell).is_file():
        return shell
    for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if Path(candidate).is_file():
            return candidate
    return "/bin/sh"


# ---------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------


def resolve_binary(token: str, cwd: Path) -> str | None:
    """Locate an executable the way the platform's shell would."""
    if not token:
        return None
    looks_like_path = "/" in token or (IS_WINDOWS and "\\" in token)
    if looks_like_path:
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.is_file():
            return str(candidate)
        if IS_WINDOWS and candidate.suffix == "":
            for ext in _pathext():
                with_ext = candidate.with_suffix(ext)
                if with_ext.is_file():
                    return str(with_ext)
        return None
    found = shutil.which(token)
    if found:
        return found
    if IS_WINDOWS:
        for ext in _pathext():
            found = shutil.which(token + ext)
            if found:
                return found
    return None


def _pathext() -> list[str]:
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [e.lower() for e in raw.split(";") if e]


_win32 = None


def _win():
    """ctypes view of the Win32 calls we need. None off Windows."""
    global _win32
    if _win32 is not None or not IS_WINDOWS:
        return _win32
    from ctypes import wintypes

    class API:
        def __init__(self):
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.k32.OpenProcess.restype = wintypes.HANDLE
            self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.k32.WaitForSingleObject.restype = wintypes.DWORD
            self.k32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.k32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]

    _win32 = API()
    return _win32


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102


def pid_alive(pid: int) -> bool:
    """True only for a live process. A zombie counts as dead on POSIX."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        api = _win()
        if api is None:
            return False
        handle = api.k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            return api.k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            api.k32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # POSIX: the pid exists. A zombie is not running, though, and treating it as
    # alive would make a crash look healthy and burn the whole readiness timeout.
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            rest = fh.read().rsplit(")", 1)[-1].split()
        if rest and rest[0] == "Z":
            return False
    except OSError:
        pass
    return True


def pid_identity(pid: int) -> str:
    """Executable path for a pid, best effort. Empty when unreadable."""
    if pid <= 0:
        return ""
    if IS_WINDOWS:
        api = _win()
        if api is None:
            return ""
        from ctypes import wintypes

        handle = api.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if api.k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            api.k32.CloseHandle(handle)
    return ""


def pid_start_time(pid: int) -> float | None:
    """Process start time as epoch seconds, or None when unreadable."""
    if pid <= 0:
        return None
    if IS_WINDOWS:
        api = _win()
        if api is None:
            return None
        from ctypes import wintypes

        handle = api.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = api.k32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None
            # FILETIME counts 100ns intervals since 1601-01-01.
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ticks / 1e7 - 11644473600
        finally:
            api.k32.CloseHandle(handle)
    return None


def kill_tree(pid: int, force: bool = False) -> bool:
    """Terminate a process and its children.

    POSIX signals the process group; Windows uses `taskkill /T`, which walks the
    child list. Both are chosen so a shell wrapper cannot leave the real service
    running as an orphan holding the port.
    """
    if pid <= 0:
        return False
    if IS_WINDOWS:
        cmd = ["taskkill", "/PID", str(int(pid)), "/T"]
        if force:
            cmd.append("/F")
        try:
            proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode != 0 and not force:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
        return True

    import signal as _signal

    sig = _signal.SIGKILL if force else _signal.SIGTERM
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid and pgid != os.getpgid(0):
        try:
            os.killpg(pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def spawn(
    argv: list[str],
    cwd: Path,
    stdout,
    env: dict,
    detached: bool = True,
) -> subprocess.Popen:
    """Start a service in its own process tree, with output going to `stdout`."""
    kwargs: dict = {
        "cwd": str(cwd),
        "stdout": stdout,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if IS_WINDOWS:
        # A new process group is what `taskkill /T` anchors on; no console window is
        # wanted because the output already goes to a file.
        kwargs["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = detached
    return subprocess.Popen(argv, **kwargs)


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------


def port_pids(port: int) -> list[int]:
    """Pids listening on a TCP port."""
    if IS_WINDOWS:
        return _port_pids_netstat(port)
    out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    pids: list[int] = []
    for tok in out.split():
        if tok.isdigit() and int(tok) not in pids:
            pids.append(int(tok))
    return pids


_NETSTAT_ROW = re.compile(r"^\s*TCP\s+(\S+)\s+(\S+)\s+LISTENING\s+(\d+)\s*$", re.I)


def _port_pids_netstat(port: int) -> list[int]:
    """Parse `netstat -ano`. Used instead of PowerShell so it works on every Windows
    edition without a profile or module load, and stays fast enough for a probe."""
    out = _run(["netstat", "-ano", "-p", "TCP"])
    pids: list[int] = []
    for line in out.splitlines():
        m = _NETSTAT_ROW.match(line)
        if not m:
            continue
        local, _remote, pid = m.groups()
        # Local address looks like 0.0.0.0:8000 or [::]:8000
        _, _, port_text = local.rpartition(":")
        if port_text.isdigit() and int(port_text) == int(port):
            value = int(pid)
            if value not in pids:
                pids.append(value)
    return pids


def _run(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None
