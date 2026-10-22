#!/usr/bin/env python3
"""devpad PoC — prove which terminal-opening channel actually works on THIS machine.

Why this exists: everything else in the design reuses code that already has test
coverage. The single unverified assumption is how a visible terminal window is
opened and closed. This script answers that, for real, with your eyes.

Run it in YOUR OWN terminal (not through an agent): an agent sandbox can block
Apple Events, which would make an otherwise healthy machine look broken.

    python3 poc/terminal_probe.py            # phase 1: read-only reconnaissance
    python3 poc/terminal_probe.py --spawn    # phase 2: open real windows
    python3 poc/terminal_probe.py --close    # phase 3: prove the close path
    python3 poc/terminal_probe.py --spawn --driver iterm   # test one driver only

Every check prints PASS / FAIL / SKIP plus the raw error, so a failure is
diagnosable instead of mysterious. Nothing is installed and nothing outside the
project is modified except temporary .command files under the system temp dir.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "probe-state.json"
LOG_DIR = HERE / "logs"

C_OK = "\033[32m"
C_BAD = "\033[31m"
C_WARN = "\033[33m"
C_DIM = "\033[2m"
C_OFF = "\033[0m"

RESULTS: list[dict] = []


def record(driver: str, step: str, status: str, detail: str = ""):
    RESULTS.append({"driver": driver, "step": step, "status": status, "detail": detail})
    icon = {"PASS": f"{C_OK}PASS{C_OFF}", "FAIL": f"{C_BAD}FAIL{C_OFF}", "SKIP": f"{C_WARN}SKIP{C_OFF}"}[status]
    print(f"  {icon} {driver}/{step}" + (f"  {C_DIM}{detail}{C_OFF}" if detail else ""))


def osascript(script: str, timeout: int = 25) -> tuple[int, str, str]:
    """Run AppleScript. Returns (rc, stdout, stderr) — never raises."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return (
            proc.returncode,
            proc.stdout.decode("utf-8", "replace").strip(),
            proc.stderr.decode("utf-8", "replace").strip(),
        )
    except subprocess.TimeoutExpired:
        return 124, "", "osascript timed out (a permission dialog may be waiting for you)"
    except OSError as exc:
        return 127, "", str(exc)


def app_installed(name: str) -> bool:
    """Is the app present? Terminal lives under /System/Applications/Utilities,
    so checking only the obvious two roots produces a false "not installed"."""
    for path in (
        f"/Applications/{name}.app",
        f"/System/Applications/{name}.app",
        f"/System/Applications/Utilities/{name}.app",
        os.path.expanduser(f"~/Applications/{name}.app"),
    ):
        if Path(path).is_dir():
            return True
    return False


def app_running(name: str) -> bool:
    proc = subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# phase 1: read-only reconnaissance (must never trigger a permission dialog)
# ---------------------------------------------------------------------------


def probe_iterm_readonly() -> bool:
    """Reconnaissance for iTerm.

    Note what is deliberately NOT used as a health signal: `get version` succeeds
    even when scripting is forbidden, because macOS answers `version` generically
    without involving iTerm at all. It is therefore a false positive and is
    reported as informational only. Any statement that touches a window or session
    is the real test.
    """
    if not app_installed("iTerm"):
        record("iterm", "installed", "SKIP", "iTerm.app not found")
        return False

    rc, out, err = osascript('tell application "iTerm" to get version')
    record(
        "iterm",
        "version",
        "PASS" if rc == 0 else "FAIL",
        f"v{out} " + (f"({err})" if rc else "") if out or err else "",
    )

    # A window/session-scoped read: this is the honest scripting-permission test.
    rc, out, err = osascript('tell application "iTerm" to get count of windows')
    if rc == 0:
        record("iterm", "scripting access", "PASS", f"{out} window(s); window-scoped reads allowed")
        return True
    if "-10004" in err or "not allowed" in err.lower() or "权限" in err:
        record(
            "iterm",
            "scripting access",
            "FAIL",
            "Automation not authorised — grant: System Settings → Privacy & Security → "
            "Automation → allow your terminal to control iTerm2 (then re-run)",
        )
    else:
        record("iterm", "scripting access", "FAIL", err or f"rc={rc}")
    return False


def probe_apple_terminal_readonly() -> bool:
    if not app_installed("Terminal"):
        record("apple-terminal", "installed", "SKIP", "Terminal.app not found")
        return False
    rc, out, err = osascript('tell application "Terminal" to get version')
    record("apple-terminal", "version", "PASS" if rc == 0 else "FAIL", f"v{out}" if out else (err or f"rc={rc}"))
    rc, out, err = osascript('tell application "Terminal" to get count of windows')
    if rc == 0:
        record("apple-terminal", "scripting access", "PASS", f"{out} window(s)")
        return True
    record("apple-terminal", "scripting access", "FAIL", err or f"rc={rc}")
    return False


def probe_tmux() -> bool:
    exe = shutil.which("tmux")
    if not exe:
        record("tmux", "installed", "SKIP", "tmux not on PATH — brew install tmux")
        return False
    proc = subprocess.run([exe, "-V"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    record("tmux", "installed", "PASS", proc.stdout.decode().strip())
    return True


def probe_command_file() -> bool:
    """The `open`-based channel needs no automation permission at all."""
    if not shutil.which("open"):
        record("command-file", "available", "FAIL", "open(1) missing")
        return False
    rc, out, err = osascript('tell application "System Events" to get name')
    record(
        "command-file",
        "available",
        "PASS",
        "uses open(1) + .command; does not need Automation permission"
        + ("" if rc == 0 else " (System Events is restricted here, which is fine)"),
    )
    return True


def recon():
    print(f"\n{C_DIM}=== phase 1: reconnaissance (read-only, no windows opened) ==={C_OFF}")
    print(f"  host: {sys.platform}  python: {sys.version.split()[0]}")
    print(f"  iTerm running: {app_running('iTerm')}   Terminal running: {app_running('Terminal')}")
    print("")
    probe_iterm_readonly()
    probe_apple_terminal_readonly()
    probe_tmux()
    probe_command_file()
    print(f"\n  {C_DIM}note: opening a window may raise a one-time "
          f"\"allow control of iTerm2\" dialog — that is expected exactly once.{C_OFF}")


# ---------------------------------------------------------------------------
# phase 2: open real windows
# ---------------------------------------------------------------------------

ENV_PROBE = """printf 'node:    %s\\n' "$(command -v node || echo 'NOT FOUND')"
printf 'python3: %s\\n' "$(command -v python3 || echo 'NOT FOUND')"
printf 'SHELL:   %s\\n' "$SHELL"
"""

DEFAULT_PROBE_CMD = "printf 'pretending to fail now...\\n'; exit 7"


def login_shell() -> str:
    """The user's login shell, so a spawned window behaves like their own terminal."""
    shell = os.environ.get("SHELL") or ""
    if shell and Path(shell).is_file():
        return shell
    for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if Path(candidate).is_file():
            return candidate
    return "/bin/sh"


def make_runner(
    title: str,
    hold: bool,
    cmd: str = DEFAULT_PROBE_CMD,
    cwd: Path | None = None,
) -> Path:
    """Write the wrapper that a spawned terminal window executes.

    This wrapper is where the two non-negotiable requirements are implemented:

      1. the service runs through a LOGIN shell (`$SHELL -lc`), because a window
         opened from an app icon does not inherit your interactive environment —
         `node` from nvm or `python3` from conda would otherwise be missing;
      2. the exit code is printed and the window is held open afterwards, so the
         log of a crashed service is still readable, selectable and copyable.
    """
    workdir = cwd or LOG_DIR
    workdir.mkdir(parents=True, exist_ok=True)
    runner = workdir / f"runner-{title}.command"
    shell = login_shell()
    # Single-quote the command for the outer shell, escaping embedded quotes.
    quoted = "'" + cmd.replace("'", "'\\''") + "'"
    body = (
        "#!/bin/sh\n"
        "# Generated by devpad's terminal probe. Safe to delete.\n"
        f"DEVPAD_TITLE={shlex.quote(title)}\n"
        f"cd {shlex.quote(str(workdir))} || exit 1\n"
        "printf '\\n\\033[1m── devpad probe: %s ──\\033[0m\\n' \"$DEVPAD_TITLE\"\n"
        "printf 'cwd: %s\\n' \"$(pwd)\"\n"
        "printf '%s\\n' '-- environment visible to this window --'\n"
        f"{ENV_PROBE}"
        "printf '%s\\n' '-- service output starts here (must stay readable after exit) --'\n"
        f"{shell} -lc {quoted}\n"
        "code=$?\n"
        "printf '\\n\\033[1m── devpad probe: exited with code %s ──\\033[0m\\n' \"$code\"\n"
        "printf '%s\\n' 'If you can still read the output above, the HOLD requirement is met.'\n"
        + (
            "printf 'Press Enter to close this window... '\nread -r _\n"
            if hold
            else ""
        )
    )
    runner.write_text(body)
    runner.chmod(0o755)
    return runner


def spawn_iterm(title: str, runner: Path) -> dict | None:
    """Create an iTerm window, then write the runner command into it.

    Two phases, deliberately: `write text` immediately after `create window` is
    silently lost, because the new session is not ready to accept input yet. The
    window opens, does nothing, and with a close-on-exit profile simply vanishes —
    which looks like a broken driver rather than a timing bug.

    The handle is the session's tty path: stable and usable as a lookup key, which
    `id` is not on iTerm 3.7.
    """
    create = '''
    tell application "iTerm"
        activate
        set w to (create window with default profile)
        tell current session of w
            return (tty of it as text)
        end tell
    end tell
    '''
    rc, out, err = osascript(create, timeout=40)
    if rc != 0 or not out:
        hint = ""
        if "-10004" in err or "not allowed" in err.lower() or "权限" in err:
            hint = (
                "automation denied — grant this host access to iTerm2 in "
                "System Settings → Privacy & Security → Automation, or use the command-file driver"
            )
        record("iterm", "spawn window", "FAIL", (err or f"rc={rc}") + (f" | {hint}" if hint else ""))
        return None
    tty = out

    # Poll until the session is idle, then type. Typing into a busy session feeds
    # the running process's stdin instead of the shell, so the command is lost.
    # iTerm has no usable `sessions of window` collection here (-1728), so access
    # the session through its window by index, each access guarded: a window can
    # legitimately have no current session and would otherwise abort the script.
    ready = f'''
    tell application "iTerm"
        repeat 40 times
            repeat with i from 1 to (count of windows)
                try
                    set s to current session of window i
                    if (tty of s as text) is "{tty}" then
                        if (is processing of s) is false then return "ready"
                    end if
                end try
            end repeat
            delay 0.1
        end repeat
        return "timeout"
    end tell
    '''
    rc, out, err = osascript(ready, timeout=20)
    if out != "ready":
        record("iterm", "window ready", "SKIP", f"proceeding after wait ({out or err})")

    write = f'''
    tell application "iTerm"
        repeat with i from 1 to (count of windows)
            try
                set s to current session of window i
                if (tty of s as text) is "{tty}" then
                    tell s to write text "exec {runner}"
                    return "written"
                end if
            end try
        end repeat
        return "not-found"
    end tell
    '''
    rc, out, err = osascript(write, timeout=30)
    if rc == 0 and out == "written":
        record("iterm", "spawn window", "PASS", f"window on {tty}, command written")
        return {"driver": "iterm", "tty": tty, "title": title}

    hint = "automation denied — check System Settings → Privacy & Security → Automation" if (
        "-10004" in err or "not allowed" in err.lower() or "权限" in err
    ) else ""
    record("iterm", "spawn window", "FAIL", f"{out} {err}".strip() + (f" | {hint}" if hint else ""))
    return None


def spawn_apple_terminal(title: str, runner: Path) -> dict | None:
    """Terminal.app: `do script` opens a window and returns a tab reference.

    Note: a tab reference is NOT a durable handle — it does not survive across
    osascript invocations. So the handle records the custom title, which is what
    the close path searches for.
    """
    script = f'''
    tell application "Terminal"
        activate
        set t to do script "exec {runner}"
        set custom title of t to "{title}"
        return "spawned"
    end tell
    '''
    rc, out, err = osascript(script, timeout=40)
    if rc == 0:
        record("apple-terminal", "spawn window", "PASS", f"window opened, title {title!r}")
        return {"driver": "apple-terminal", "title": title}
    hint = ""
    if "-10004" in err or "not allowed" in err.lower() or "权限" in err:
        hint = (
            "permission denied — grant: System Settings → Privacy & Security → Automation → "
            "allow your terminal (or the agent host) to control Terminal"
        )
    record("apple-terminal", "spawn window", "FAIL", (err or f"rc={rc}") + (f" | {hint}" if hint else ""))
    return None


def spawn_command_file(title: str, runner: Path) -> dict | None:
    """No Automation permission: hand the .command to `open`."""
    proc = subprocess.run(
        ["open", "-a", "Terminal", str(runner)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        record("command-file", "spawn window", "PASS", "opened via `open -a Terminal` (no Apple Events)")
        # No window id is available on this channel; the PID is found by matching the cmdline.
        time.sleep(1.2)
        return {"driver": "command-file", "windowId": None, "title": title, "runner": str(runner)}
    record("command-file", "spawn window", "FAIL", out or f"rc={proc.returncode}")
    return None


def spawn_vscode(title: str, runner: Path) -> dict | None:
    """VS Code cannot be driven this way, and the probe says so instead of pretending.

    Verified: `code --help` exposes no way to open an integrated terminal running a
    command (`--locate-shell-integration-path` only prints a path). Driving VS Code
    terminals requires the extension API, so this is a separate deliverable
    (a thin extension reusing the same core), not a CLI driver.
    """
    record(
        "vscode",
        "spawn window",
        "SKIP",
        "not possible from the CLI (verified against `code --help`); needs a VS Code extension",
    )
    return None


def spawn_tmux(title: str, runner: Path) -> dict | None:
    """tmux: a persistent session, then an iTerm window attached to it."""
    exe = shutil.which("tmux")
    if not exe:
        record("tmux", "spawn window", "SKIP", "tmux not installed (brew install tmux)")
        return None
    session = f"devpad-{title}"
    subprocess.run([exe, "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.run(
        [exe, "new-session", "-d", "-s", session, "-c", str(LOG_DIR), f"exec {runner}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        record("tmux", "spawn session", "FAIL", proc.stdout.decode().strip())
        return None
    record("tmux", "spawn session", "PASS", f"detached session {session} (survives closing the window)")

    if not app_installed("iTerm"):
        return {"driver": "tmux", "session": session, "title": title}

    script = f'''
    tell application "iTerm"
        activate
        set w to (create window with default profile)
        set theTty to ""
        tell current session of w
            write text "tmux attach -t {session}"
            set theTty to (tty of it as text)
        end tell
        return theTty
    end tell
    '''
    rc, out, err = osascript(script, timeout=40)
    if rc == 0:
        record("tmux", "attach window", "PASS", f"iTerm window on {out} attached to {session}")
        return {"driver": "tmux", "tty": out, "session": session, "title": title}
    record("tmux", "attach window", "FAIL", err or f"rc={rc}")
    return {"driver": "tmux", "session": session, "title": title}


SPAWNERS = {
    "iterm": spawn_iterm,
    "apple-terminal": spawn_apple_terminal,
    "command-file": spawn_command_file,
    "vscode": spawn_vscode,
    "tmux": spawn_tmux,
}


def do_spawn(drivers: list[str], hold: bool, keep: bool):
    print(f"\n{C_DIM}=== phase 2: opening real windows ==={C_OFF}")
    print(f"  hold={'on' if hold else 'off'}   drivers={', '.join(drivers)}")
    print(f"  {C_WARN}A window will open per driver. It will print output, then exit 7.{C_OFF}")
    print(f"  {C_WARN}Check, in each window: can you read the output AFTER the process died?{C_OFF}\n")
    handles = _load_state()
    for name in drivers:
        fn = SPAWNERS.get(name)
        if not fn:
            record(name, "spawn window", "SKIP", "unknown driver")
            continue
        title = name
        # A short-lived command that exits non-zero: the window's final state is a
        # crashed service, which is exactly the case the design must handle.
        runner = make_runner(
            title, hold=hold, cmd="sleep 3; printf 'pretending to fail now...\\n'; exit 7"
        )
        handle = fn(title, runner)
        if handle:
            handles.append(handle)
            print(f"    {C_DIM}runner: {runner}{C_OFF}")
        time.sleep(0.8)
    _save_state(handles)
    print(f"\n  {C_DIM}state written to {STATE} — run with --close to clean up, "
          f"or --close --keep to leave the windows for inspection.{C_OFF}")


# ---------------------------------------------------------------------------
# phase 3: prove the close path
# ---------------------------------------------------------------------------


def close_iterm(handle: dict) -> bool:
    """Close the window identified by its tty path (the handle spawn_iterm returns).

    Uses the same index-based session access as spawn: iTerm does not expose a
    usable `sessions of window` collection, and `id of window` is not dependable.
    """
    needle = handle.get("tty") or ""
    if not needle:
        record("iterm", "close window", "SKIP", "no tty recorded for this handle")
        return False
    script = f'''
    tell application "iTerm"
        repeat with i from 1 to (count of windows)
            try
                set s to current session of window i
                if (tty of s as text) is "{needle}" then
                    close window i
                    return "closed"
                end if
            end try
        end repeat
        return "not-found"
    end tell
    '''
    rc, out, err = osascript(script, timeout=25)
    if rc == 0 and out == "closed":
        record("iterm", "close window", "PASS", f"closed window on {needle}")
        return True
    record("iterm", "close window", "FAIL", f"{out} {err}".strip() or f"rc={rc}")
    return False


def close_apple_terminal(handle: dict) -> bool:
    # Terminal.app tab ids are not stable addresses across script runs; match by
    # the custom title we set, which is the documented-ish way to find it again.
    title = handle.get("title", "")
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            repeat with t in tabs of w
                if custom title of t is "{title}" then
                    close w saving no
                    return "closed"
                end if
            end repeat
        end repeat
        return "not-found"
    end tell
    '''
    rc, out, err = osascript(script, timeout=25)
    if rc == 0 and out == "closed":
        record("apple-terminal", "close window", "PASS", f"closed by custom title {title!r}")
        return True
    record("apple-terminal", "close window", "FAIL", f"{out} {err}".strip() or f"rc={rc}")
    return False


def close_tmux(handle: dict) -> bool:
    exe = shutil.which("tmux")
    session = handle.get("session")
    if not exe or not session:
        return False
    proc = subprocess.run([exe, "kill-session", "-t", session], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = proc.returncode == 0
    record("tmux", "kill session", "PASS" if ok else "FAIL", proc.stdout.decode().strip() or session)
    return ok


def close_command_file(handle: dict) -> bool:
    """No window handle exists, so close by finding the runner's processes."""
    runner = handle.get("runner")
    if not runner:
        return False
    proc = subprocess.run(["pgrep", "-f", runner], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pids = proc.stdout.decode().split()
    if not pids:
        record("command-file", "close window", "SKIP", "no process found for the runner")
        return False
    for pid in pids:
        try:
            os.kill(int(pid), 15)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    record(
        "command-file",
        "close window",
        "PASS",
        f"signalled {len(pids)} pid(s); the window itself must be closed by the user "
        "(no addressable handle on this channel)",
    )
    return True


CLOSERS = {
    "iterm": close_iterm,
    "apple-terminal": close_apple_terminal,
    "tmux": close_tmux,
    "command-file": close_command_file,
    "vscode": lambda h: False,
}


def do_close(keep: bool):
    print(f"\n{C_DIM}=== phase 3: proving the close path ==={C_OFF}")
    handles = _load_state()
    if not handles:
        print(f"  {C_WARN}no recorded handles — run --spawn first{C_OFF}")
        return
    print(f"  {C_DIM}{len(handles)} handle(s) recorded{C_OFF}\n")
    for handle in handles:
        driver = handle.get("driver", "?")
        closer = CLOSERS.get(driver)
        if not closer:
            record(driver, "close window", "SKIP", "no closer implemented")
            continue
        closer(handle)
    if not keep:
        STATE.unlink(missing_ok=True)
        print(f"\n  {C_DIM}state cleared{C_OFF}")


# ---------------------------------------------------------------------------


def _load_state() -> list[dict]:
    if STATE.is_file():
        try:
            data = json.loads(STATE.read_text())
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []
    return []


def _save_state(handles: list[dict]):
    STATE.write_text(json.dumps(handles, indent=2))


def summary():
    print(f"\n{C_DIM}=== summary ==={C_OFF}")
    if not RESULTS:
        return
    by_driver: dict[str, list[dict]] = {}
    for r in RESULTS:
        by_driver.setdefault(r["driver"], []).append(r)
    for driver, rows in by_driver.items():
        fails = [r for r in rows if r["status"] == "FAIL"]
        skips = [r for r in rows if r["status"] == "SKIP"]
        verdict = "usable" if not fails else "NOT usable"
        if skips and not fails and all(r["status"] == "SKIP" for r in rows):
            verdict = "unavailable"
        colour = C_OK if verdict == "usable" else (C_WARN if verdict == "unavailable" else C_BAD)
        print(f"  {colour}{driver:16}{C_OFF} {verdict}")
        for r in fails:
            print(f"      {C_BAD}└ {r['step']}: {r['detail'][:110]}{C_OFF}")
    print("")
    print(f"  {C_DIM}Set the winner in devpad.yaml:  terminal: {{ driver: <name> }}{C_OFF}")
    print(f"  {C_DIM}A driver that needs Automation permission will prompt once, on first spawn.{C_OFF}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prove which terminal-opening channel works here.")
    parser.add_argument("--spawn", action="store_true", help="phase 2: open real windows")
    parser.add_argument("--close", action="store_true", help="phase 3: close the windows it opened")
    parser.add_argument("--keep", action="store_true", help="with --close, leave state on disk")
    parser.add_argument("--hold", action="store_true", default=True, help="keep windows open (default)")
    parser.add_argument("--no-hold", dest="hold", action="store_false")
    parser.add_argument(
        "--driver",
        action="append",
        choices=sorted(SPAWNERS),
        help="restrict to one driver (repeatable); default: all",
    )
    args = parser.parse_args()

    print(f"devpad terminal probe  {C_DIM}({HERE}){C_OFF}")

    if not (args.spawn or args.close):
        recon()
        print(f"\n  next: {C_OK}python3 {Path(__file__).name} --spawn{C_OFF}\n")
        summary()
        return 0

    if args.spawn:
        drivers = args.driver or ["iterm", "apple-terminal", "command-file", "tmux", "vscode"]
        do_spawn(drivers, hold=args.hold, keep=args.keep)

    if args.close:
        do_close(keep=args.keep)

    summary()
    return 1 if any(r["status"] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
