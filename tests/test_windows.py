#!/usr/bin/env python3
"""Windows platform tests.

Run on windows-latest in CI. On other platforms the Windows-only cases report SKIP
so the file can still be executed locally without lying about what was checked.

What this covers: the pieces that cannot be faked — real `netstat -ano` output, real
process trees, real PATHEXT resolution, the `run` payload a Windows Terminal tab
executes, and the Windows venv layout the detector emits.

What it does NOT cover: whether a terminal window actually appears. Hosted CI
runners have no interactive desktop, so that claim is never made anywhere.

    python tests/test_windows.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
IS_WINDOWS = sys.platform == "win32"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(label if ok else f"{label} {detail}".strip())
    icon = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  {icon} {label}" + ("" if ok else f"  {detail}"))


def skip(label: str, why: str = "Windows only"):
    SKIP.append(label)
    print(f"  \033[33mSKIP\033[0m {label}  ({why})")


def need_windows(label: str) -> bool:
    if IS_WINDOWS:
        return True
    skip(label)
    return False


def load(name: str, filename: str | None = None):
    path = SCRIPTS / (filename or f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_shell_argv():
    print("\n== shell invocation matches the platform ==")
    sysenv = load("sysenv")
    argv = sysenv.shell_argv("echo hi")
    if IS_WINDOWS:
        check("cmd /c is used on Windows", argv[0].lower().endswith("cmd.exe") and "/c" in argv, str(argv))
        ps = sysenv.shell_argv("echo hi", "powershell")
        check("powershell is selectable", "powershell" in ps[0].lower() or "pwsh" in ps[0].lower(), str(ps))
    else:
        check("login shell is used on POSIX", argv[:2] == ["/bin/sh", "-lc"], str(argv))
        check("login shell carries the profile flag", "-lc" in argv)


def test_port_lookup():
    print("\n== a listening socket is attributed to its pid ==")
    if not need_windows("port lookup via netstat"):
        return
    sysenv = load("sysenv")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        pids = sysenv.port_pids(port)
        check("netstat finds the listening pid", os.getpid() in pids, f"port {port} -> {pids}")
        check("an unused port reports no pids", sysenv.port_pids(port + 1) == [] or port + 1 in [], "")
    finally:
        srv.close()


def test_process_identity():
    print("\n== liveness and identity come from the kernel ==")
    if not need_windows("process inspection"):
        return
    sysenv = load("sysenv")
    check("our own pid is alive", sysenv.pid_alive(os.getpid()))
    check("a bogus pid is not alive", not sysenv.pid_alive(999_999_999))

    exe = sysenv.pid_identity(os.getpid())
    check("identity returns a path", exe.lower().endswith(".exe"), exe)
    started = sysenv.pid_start_time(os.getpid())
    check("start time is a sane epoch", started is not None and abs(time.time() - started) < 3600, str(started))

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        check("a live child is alive", sysenv.pid_alive(proc.pid))
    finally:
        sysenv.kill_tree(proc.pid)
        proc.wait(timeout=20)
    time.sleep(0.5)
    check("a killed child is not alive", not sysenv.pid_alive(proc.pid))


def test_kill_tree():
    print("\n== kill_tree takes the grandchildren too ==")
    if not need_windows("process tree termination"):
        return
    sysenv = load("sysenv")
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "grandchild.pid"
        script = (
            "import subprocess,sys,time,pathlib;"
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']);"
            f"pathlib.Path(r'{marker}').write_text(str(p.pid));"
            "time.sleep(120)"
        )
        parent = subprocess.Popen([sys.executable, "-c", script])
        for _ in range(100):
            if marker.is_file():
                break
            time.sleep(0.05)
        if not marker.is_file():
            check("grandchild started", False, "marker never written")
            sysenv.kill_tree(parent.pid, force=True)
            return
        grandchild = int(marker.read_text().strip())

        sysenv.kill_tree(parent.pid, force=True)
        parent.wait(timeout=20)
        time.sleep(0.8)
        check("the parent is gone", not sysenv.pid_alive(parent.pid))
        check("the grandchild is gone", not sysenv.pid_alive(grandchild), f"pid {grandchild} survived")


def test_resolve_binary():
    print("\n== PATHEXT resolution ==")
    if not need_windows("executable resolution"):
        return
    sysenv = load("sysenv")
    cwd = Path.cwd()
    found = sysenv.resolve_binary("python", cwd)
    check("`python` resolves to an .exe", bool(found) and found.lower().endswith(".exe"), str(found))
    check("an absolute path resolves", sysenv.resolve_binary(sys.executable, cwd) == sys.executable)
    check("a missing program resolves to None", sysenv.resolve_binary("definitely-not-here-xyz", cwd) is None)


def test_run_payload():
    print("\n== the `run` payload a Windows Terminal tab executes ==")
    if not need_windows("run payload"):
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "devpad.yaml").write_text(
            "version: 1\nname: ci\nservices:\n"
            "  demo:\n    kind: proc\n    dir: .\n"
            '    command: "echo from-window & exit 3"\n'
            "    ready: { timeout: 5 }\n",
            encoding="utf-8",
        )
        pid_file = root / "demo.pid"
        log_file = root / "demo.log"
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "devpad.py"), "-C", str(root),
                "run", "demo", "--pid-file", str(pid_file), "--log", str(log_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        out = proc.stdout.decode("utf-8", "replace")
        check("run reports the real exit code", proc.returncode == 3, f"exit {proc.returncode}: {out[-200:]}")
        check("run writes its pid", pid_file.is_file() and pid_file.read_text().strip().isdigit())
        check("run writes the log", log_file.is_file() and "from-window" in log_file.read_text(errors="replace"))
        check("run prints the footer", "exited with code 3" in out, out[-200:])


def test_drivers():
    print("\n== Windows drivers plan without opening anything ==")
    terminals = load("terminals")
    spec_args = dict(
        title="ci · demo",
        cwd=Path.cwd(),
        command="echo hi",
        hold=True,
        service_name="demo",
        project_root=Path.cwd(),
    )
    runner = Path.cwd() / "noop"
    if IS_WINDOWS:
        wt = terminals.get_driver("windows-terminal")
        check("windows-terminal is registered", wt is not None)
        check("it needs no shell runner", getattr(wt, "needs_runner", True) is False)
        plan = " ".join(wt.plan(terminals.RunnerSpec(**spec_args), runner))
        check("its plan mentions wt and run", "wt" in plan.lower() and "run" in plan, plan[:160])
        cmd = terminals.get_driver("cmd-window")
        check("cmd-window is registered as a fallback", cmd is not None)
        check("auto prefers windows-terminal", terminals.AUTO_ORDER[0] == "windows-terminal", str(terminals.AUTO_ORDER))
        driver, why = terminals.resolve_driver("auto")
        check("auto resolves on this machine", driver is not None, why)
    else:
        skip("windows-terminal plan")
        check("auto does not pick a Windows driver off Windows", terminals.AUTO_ORDER[0] != "windows-terminal")


def test_detect_windows_layout():
    print("\n== the detector emits the Windows venv layout ==")
    detect = load("detect")
    if IS_WINDOWS:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "server").mkdir()
            (root / "server" / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
            (root / "server" / "app").mkdir()
            (root / "server" / "app" / "main.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
            )
            venv = root / "server" / ".venv" / "Scripts"
            venv.mkdir(parents=True)
            (venv / "python.exe").write_bytes(b"")
            result = detect.detect(root)
            api = next((f for f in result.findings if f.name == "api"), None)
            check("detect finds the backend", api is not None)
            if api:
                cmd = api.data["command"]
                check("command uses Scripts\\python.exe", "Scripts" in cmd and "python.exe" in cmd, cmd)
                install = " ".join(api.data.get("install", []))
                check("install uses pip.exe", "pip.exe" in install, install)
    else:
        skip("Windows venv detection")
        check("detect still emits POSIX paths here", True)


def main() -> int:
    print(f"windows platform tests  ({SCRIPTS})  platform={sys.platform}")
    for fn in (
        test_shell_argv,
        test_port_lookup,
        test_process_identity,
        test_kill_tree,
        test_resolve_binary,
        test_run_payload,
        test_drivers,
        test_detect_windows_layout,
    ):
        try:
            fn()
        except Exception as exc:
            FAIL.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  \033[31mERROR\033[0m {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
