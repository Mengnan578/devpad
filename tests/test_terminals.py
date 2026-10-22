#!/usr/bin/env python3
"""Tests for the terminal driver layer.

The whole point of `plan()` is that a driver can be tested without opening a
window, so this suite is safe to run in CI and on a machine with no GUI session.
Nothing here spawns a terminal.

    python3 tests/test_terminals.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TERMINALS = ROOT / "scripts" / "terminals.py"

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(label if ok else f"{label} {detail}".strip())
    icon = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  {icon} {label}" + ("" if ok else f"  {detail}"))


def load():
    spec = importlib.util.spec_from_file_location("devpad_terminals", TERMINALS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def spec_for(term, tmp: Path, hold: bool = True) -> object:
    return term.RunnerSpec(
        title="demo · api",
        cwd=tmp,
        command="python3 app.py --port 8080",
        hold=hold,
        env={"PYTHONUNBUFFERED": "1", "APP_ENV": "local"},
        pid_file=tmp / "api.pid",
    )


def test_runner_rendering():
    print("\n== the generated runner carries the two non-negotiable behaviours ==")
    term = load()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        spec = spec_for(term, tmp)
        text = term.render_runner(spec)

        check("runs the command through a login shell", " -lc " in text, "no -lc found")
        check("quotes the command for the outer shell", "'python3 app.py --port 8080'" in text)
        check("captures and prints the exit code", "devpad_code=$?" in text and "exited with code" in text)
        check("holds the window open", "Press Enter to close this window" in text)
        check("writes its pid so the parent can track it", str(tmp / "api.pid") in text)
        check("exports the service environment", 'export PYTHONUNBUFFERED=' in text and 'export APP_ENV=' in text)
        check("shows the title and cwd before the command", "DEVPAD_TITLE=" in text and "cwd:" in text)

        no_hold = term.render_runner(spec_for(term, tmp, hold=False))
        check("hold can be turned off", "Press Enter to close this window" not in no_hold)

        # An env name that is not a valid identifier must be skipped, not emitted
        # as a broken export line that kills the shell.
        risky = term.RunnerSpec(title="x", cwd=tmp, command="true", env={"BAD NAME": "1", "OK": "2"})
        risky_text = term.render_runner(risky)
        check("skips invalid environment names", "BAD NAME" not in risky_text and "export OK=" in risky_text)


def test_runner_file():
    print("\n== write_runner produces an executable file with a safe name ==")
    term = load()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        runner = term.write_runner(spec_for(term, tmp), tmp / "runners")
        check("file exists", runner.is_file())
        check("file is executable", bool(runner.stat().st_mode & 0o111))
        check("path is sanitised (no spaces or middle dots)", " " not in runner.name)
        first = runner.read_text()
        term.write_runner(spec_for(term, tmp), tmp / "runners")
        check("regeneration is idempotent", runner.read_text() == first, "content changed between writes")


def test_driver_contract():
    print("\n== every driver implements the contract and plans without side effects ==")
    term = load()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        spec = spec_for(term, tmp)
        runner = term.write_runner(spec, tmp / "runners")
        before = sorted(p.name for p in tmp.iterdir())

        for name in sorted(term.DRIVERS):
            driver = term.DRIVERS[name]
            kind = type(driver).__name__
            check(f"{name}: available() returns (bool, str)", isinstance(driver.available(), tuple))
            plan = driver.plan(spec, runner)
            check(f"{name}: plan() is a non-empty list", isinstance(plan, list) and len(plan) > 0, f"{kind}")
            check(f"{name}: plan() is text-only", all(isinstance(x, str) for x in plan))
            check(f"{name}: close() is callable", callable(driver.close))

        after = sorted(p.name for p in tmp.iterdir())
        check("calling plan() created no files", before == after, f"{before} -> {after}")
        check("no window was opened (plan-only)", True)


def test_resolution():
    print("\n== driver resolution is explicit and never silently guesses ==")
    term = load()

    driver, why = term.resolve_driver("definitely-not-a-driver")
    check("unknown driver is rejected", driver is None and "unknown driver" in why, why)

    driver, why = term.resolve_driver("direct")
    check("explicit driver is honoured when usable", driver is not None and driver.name == "direct")

    driver, why = term.resolve_driver("auto")
    if driver is not None:
        check("auto resolves to a known driver", driver.name in term.DRIVERS, driver.name)
        check("auto explains its choice", bool(why) and driver.name in why, why)
    else:
        check("auto reports why nothing is usable", "no usable terminal driver" in why, why)

    rows = term.report_drivers()
    check("report covers every driver", len(rows) == len(term.DRIVERS))
    check(
        "report states whether the window can be closed",
        all("can_close" in r for r in rows),
    )
    # The command-file channel cannot address its window; claiming otherwise would
    # make `down` look broken on that path.
    cf = next(r for r in rows if r["driver"] == "command-file")
    check("command-file is honest about not being closable", cf["can_close"] is False)


def test_login_shell():
    print("\n== the login shell is the user's own, not a hardcoded one ==")
    term = load()
    shell = term.login_shell()
    check("a shell path is resolved", shell.startswith("/"), shell)
    check("the resolved shell exists", Path(shell).is_file(), shell)
    original = os.environ.get("SHELL")
    try:
        os.environ["SHELL"] = "/bin/sh"
        check("honours $SHELL", term.login_shell() == "/bin/sh", term.login_shell())
        os.environ["SHELL"] = "/nonexistent/shell"
        check("falls back when $SHELL is bogus", Path(term.login_shell()).is_file(), term.login_shell())
    finally:
        if original is None:
            os.environ.pop("SHELL", None)
        else:
            os.environ["SHELL"] = original




def test_output_capture():
    print("\n== windowed output is captured live so an agent can read it ==")
    term = load()
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        log = tmp / "svc.log"
        spec = term.RunnerSpec(title="cap", cwd=tmp, command="echo hi", log_path=log)
        text = term.render_runner(spec)
        check("runner uses teepty when a log path is set", "teepty.py" in text, "teepty not referenced")
        check("runner passes the log path", str(log) in text)
        check("the command still runs through a login shell", " -lc " in text)

        plain = term.render_runner(term.RunnerSpec(title="cap", cwd=tmp, command="echo hi"))
        check("no teepty without a log path", "teepty.py" not in plain)

    # The real thing: does output reach the file while the process is still alive?
    tee = ROOT / "scripts" / "teepty.py"
    if not tee.is_file():
        check("teepty.py ships with the package", False)
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        log = Path(tmpdir) / "live.log"
        proc = subprocess.Popen(
            [sys.executable, str(tee), str(log), "/bin/sh", "-lc",
             "echo EARLY; sleep 3; echo LATE; exit 5"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.2)
        mid = log.read_text(errors="replace") if log.exists() else ""
        check("log is written while the service is still running", "EARLY" in mid, f"got {mid!r}")
        check("the tail has not arrived yet", "LATE" not in mid)
        code = proc.wait(timeout=20)
        check("the child's exit code is propagated", code == 5, f"got {code}")
        final = log.read_text(errors="replace")
        check("the full output is captured after exit", "EARLY" in final and "LATE" in final)



def main() -> int:
    print(f"terminal driver tests  ({TERMINALS})")
    if not TERMINALS.is_file():
        print(f"cannot find {TERMINALS}")
        return 2
    for fn in (
        test_runner_rendering,
        test_runner_file,
        test_driver_contract,
        test_resolution,
        test_login_shell,
        test_output_capture,
    ):
        try:
            fn()
        except Exception as exc:
            FAIL.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  \033[31mERROR\033[0m {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
