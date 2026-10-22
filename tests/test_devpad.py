#!/usr/bin/env python3
"""End-to-end tests for devpad. No third-party packages, no docker required.

    python3 tests/test_devpad.py

Exercises the parts that decide whether this tool is trustworthy: dependency
ordering, targeted restarts that leave neighbours alone, process-group cleanup,
port guarding, config validation, and the stdlib-only YAML fallback.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEVCTL = HERE.parent / "scripts" / "devpad.py"
PY = sys.executable

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(label)
        print(f"  \033[32mPASS\033[0m {label}")
    else:
        FAIL.append(f"{label} {detail}".strip())
        print(f"  \033[31mFAIL\033[0m {label} {detail}")


# ESC may already have been consumed in transit, leaving a bare "[31m" behind,
# so the escape byte is optional here.
ANSI = re.compile(r"\x1b?\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def run(*args, cwd=None, timeout=120, env=None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        [PY, str(DEVCTL), *[str(a) for a in args]],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        env=full_env,
    )
    # Colour codes are for humans; assertions must not fight them.
    return proc.returncode, strip_ansi(proc.stdout.decode("utf-8", "replace"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


SERVER_PY = """\
import http.server, json, os, sys
PORT = int(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"pid": os.getpid()}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        print("served", self.path, flush=True)
http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
"""


class Sandbox:
    def __init__(self, name: str):
        self.root = Path(tempfile.mkdtemp(prefix=f"devpad-{name}-"))
        self.ports: list[int] = []

    def write_server(self, rel: str = "server/app.py") -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SERVER_PY)
        return rel

    def next_port(self) -> int:
        p = free_port()
        self.ports.append(p)
        return p

    def config(self, body: str, path: str = "devpad.yaml"):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body))
        return p

    def state(self, service: str) -> dict | None:
        f = self.root / ".devpad" / "run" / f"{service}.json"
        if not f.is_file():
            return None
        return json.loads(f.read_text())

    def cleanup(self):
        run("down", cwd=self.root, timeout=90)
        for p in self.ports:
            subprocess.run(["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN", "-t"], stdout=subprocess.DEVNULL)
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN", "-t"], stdout=subprocess.PIPE
            ).stdout.split()
            for pid in out:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError, PermissionError):
                    pass
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------


def test_lifecycle():
    print("\n== lifecycle: order, idempotence, targeted restart, group cleanup ==")
    sb = Sandbox("life")
    try:
        p_api, p_app = sb.next_port(), sb.next_port()
        sb.write_server("server/app.py")
        sb.write_server("mobile/app.py")
        sb.config(
            f"""
            version: 1
            services:
              api:
                kind: proc
                dir: ./server
                command: python3 app.py {p_api}
                port: {p_api}
                ready: {{ tcp: {p_api}, timeout: 25 }}
              app:
                kind: proc
                dir: ./mobile
                command: python3 app.py {p_app}
                port: {p_app}
                depends_on: [api]
                ready: {{ tcp: {p_app}, timeout: 25 }}
            """
        )

        rc, out = run("up", cwd=sb.root)
        check("up exits 0", rc == 0, out[-400:])
        check("dependency order respected (api before app)", out.index("> api:") < out.index("> app:"))
        check("readiness reported for api", "ready (tcp:" in out)
        api1 = sb.state("api")
        app1 = sb.state("app")
        check("api recorded with a pid", bool(api1 and api1.get("pid")))
        check("both ports listening", port_busy(p_api) and port_busy(p_app))

        rc, out = run("up", cwd=sb.root)
        check("up is idempotent (no second start)", "already running" in out and rc == 0)
        check("pids unchanged by repeated up", sb.state("api")["pid"] == api1["pid"])

        rc, out = run("restart", "api", cwd=sb.root)
        check("targeted restart exits 0", rc == 0, out[-300:])
        check("api pid changed", sb.state("api")["pid"] != api1["pid"])
        check("app was NOT restarted", sb.state("app")["pid"] == app1["pid"])
        check("app still listening", port_busy(p_app))
        check("old api pid is gone", not _alive(api1["pid"]))

        rc, out = run("down", cwd=sb.root)
        check("down exits 0", rc == 0, out[-300:])
        time.sleep(0.5)
        check("api port released", not port_busy(p_api))
        check("app port released", not port_busy(p_app))
        check("state files cleared", sb.state("api") is None and sb.state("app") is None)
    finally:
        sb.cleanup()


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def test_process_group_cleanup():
    print("\n== process groups: killing a wrapper must not orphan its child ==")
    sb = Sandbox("pg")
    try:
        port = sb.next_port()
        sb.write_server("w/app.py")
        sb.config(
            f"""
            version: 1
            services:
              wrapped:
                kind: proc
                dir: ./w
                # `sh -c` wrapping a background-ish grandchild: the classic orphan source
                command: sh -c 'python3 app.py {port}'
                port: {port}
                ready: {{ tcp: {port}, timeout: 25 }}
            """
        )
        rc, out = run("up", cwd=sb.root)
        check("wrapped service starts", rc == 0, out[-300:])
        state = sb.state("wrapped")
        check("pgid recorded", bool(state and state.get("pgid")))
        check("wrapper flagged or matched", state is not None)
        rc, out = run("down", cwd=sb.root)
        time.sleep(0.6)
        check("port freed after down (no orphan)", not port_busy(port), f"still busy: {out[-200:]}")
    finally:
        sb.cleanup()


def test_port_guard_and_kill():
    print("\n== port conflicts: refuse to double-start, then recover ==")
    sb = Sandbox("port")
    try:
        port = sb.next_port()
        sb.write_server("s/app.py")
        sb.config(
            f"""
            version: 1
            services:
              api:
                kind: proc
                dir: ./s
                command: python3 app.py {port}
                port: {port}
                ready: {{ tcp: {port}, timeout: 20 }}
            """
        )
        squatter = subprocess.Popen([PY, str(sb.root / "s" / "app.py"), str(port)])
        for _ in range(60):
            if port_busy(port):
                break
            time.sleep(0.05)

        rc, out = run("up", cwd=sb.root)
        check("up fails when the port is taken", rc != 0)
        check("conflict names the holder pid", str(squatter.pid) in out, out[-300:])

        rc, out = run("ports", cwd=sb.root)
        check("ports lists the holder", str(port) in out and str(squatter.pid) in out)

        rc, out = run("kill", str(port), cwd=sb.root)
        check("kill frees the port", not port_busy(port), out[-200:])

        rc, out = run("up", cwd=sb.root)
        check("up succeeds after the port is freed", rc == 0, out[-300:])
    finally:
        try:
            squatter.kill()
        except Exception:
            pass
        sb.cleanup()


def test_stale_state_is_self_healing():
    print("\n== stale state: a deleted pid file must not cause a double start ==")
    sb = Sandbox("stale")
    try:
        port = sb.next_port()
        sb.write_server("s/app.py")
        sb.config(
            f"""
            version: 1
            services:
              api:
                kind: proc
                dir: ./s
                command: python3 app.py {port}
                port: {port}
                ready: {{ tcp: {port}, timeout: 20 }}
            """
        )
        run("up", cwd=sb.root)
        real_pid = sb.state("api")["pid"]
        (sb.root / ".devpad" / "run" / "api.json").unlink()

        rc, out = run("status", cwd=sb.root)
        check("status shows the port holder even with no state", str(port) in out)

        rc, out = run("up", cwd=sb.root)
        check("up refuses to double-start on the busy port", rc != 0 and "already in use" in out, out[-300:])
        check("the original process was not killed", _alive(real_pid))
    finally:
        sb.cleanup()


def test_config_validation():
    print("\n== config validation: bad input must fail loudly and early ==")
    sb = Sandbox("cfg")
    try:
        sb.config("version: 1\nservices:\n  a:\n    kind: proc\n    command: echo hi\n    depends_on: [nope]\n")
        rc, out = run("status", cwd=sb.root)
        check("unknown depends_on rejected", rc == 2 and "unknown service" in out, out[-200:])

        sb.config(
            "version: 1\nservices:\n  a:\n    kind: proc\n    command: echo a\n    depends_on: [b]\n"
            "  b:\n    kind: proc\n    command: echo b\n    depends_on: [a]\n"
        )
        rc, out = run("status", cwd=sb.root)
        check("dependency cycle rejected", rc == 2 and "cycle" in out, out[-200:])

        sb.config("version: 1\nservices:\n  a:\n    kind: nonsense\n    command: echo a\n")
        rc, out = run("status", cwd=sb.root)
        check("unknown kind rejected", rc == 2 and "unknown kind" in out, out[-200:])

        sb.config("version: 1\nservices: {}\n")
        rc, out = run("status", cwd=sb.root)
        check("empty services rejected", rc == 2, out[-200:])

        rc, out = run("status", cwd=Path(tempfile.mkdtemp(prefix="devpad-empty-")))
        check("missing config gives a clear message", rc == 2 and "no devpad config" in out, out[-200:])
    finally:
        sb.cleanup()


def test_stdlib_only_and_discovery():
    print("\n== stdlib-only path and config discovery from a subdirectory ==")
    sb = Sandbox("disc")
    try:
        port = sb.next_port()
        sb.write_server("s/app.py")
        sb.config(
            f"""
            version: 1
            services:
              api:
                kind: proc
                dir: ./s
                command: python3 app.py {port}
                port: {port}
                ready: {{ tcp: {port}, timeout: 20 }}
            """,
            path=".devpad/config.yaml",
        )
        rc, out = run("status", cwd=sb.root / "s")
        check("config found from a subdirectory", rc == 0 and "api" in out, out[-200:])

        # -S drops site-packages, so `import yaml` fails and the mini parser runs.
        proc = subprocess.run(
            [PY, "-S", str(DEVCTL), "status"],
            cwd=str(sb.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        out = proc.stdout.decode()
        check("runs without PyYAML (stdlib parser)", proc.returncode == 0 and "api" in out, out[-200:])

        env_rc, env_out = run("status", cwd=sb.root, env={"DEVPAD_CONFIG": str(sb.root / ".devpad/config.yaml")})
        check("DEVPAD_CONFIG override works", env_rc == 0 and "api" in env_out)
    finally:
        sb.cleanup()


def test_optional_and_cmd_and_rebuild_plan():
    print("\n== optional services, one-shot cmd steps, rebuild plan ==")
    sb = Sandbox("opt")
    try:
        port = sb.next_port()
        sb.write_server("s/app.py")
        marker = sb.root / "migrated.txt"
        sb.config(
            f"""
            version: 1
            defaults:
              stop_timeout: 5
            services:
              api:
                kind: proc
                dir: ./s
                command: python3 app.py {port}
                port: {port}
                ready: {{ tcp: {port}, timeout: 20 }}
                clean_globs: ["junk"]
                install: ["echo installed"]
                build: ["echo built"]
              broken:
                kind: proc
                dir: ./s
                command: node not-a-real-file.js
                optional: true
              migrate:
                kind: cmd
                dir: ./s
                command: touch {marker}
                depends_on: [api]
            """
        )
        junk = sb.root / "s" / "junk"
        junk.mkdir()

        rc, out = run("up", cwd=sb.root)
        check("up succeeds despite an optional failure", rc == 0, out[-400:])
        check("optional service reported down", "broken" in out)

        rc, out = run("start", "migrate", cwd=sb.root)
        check("cmd service runs", marker.is_file(), out[-200:])
        check("depends_on pulled api into the run", "api" in out)

        rc, out = run("rebuild", "api", "--dry-run", cwd=sb.root)
        check("rebuild dry-run prints the clean step", "rm -rf s/junk" in out, out[-400:])
        check("dry-run does not delete", junk.is_dir())
        check("dry-run lists install and build", "installed" in out and "built" in out)
    finally:
        sb.cleanup()


def test_status_json():
    print("\n== status --json is machine readable ==")
    sb = Sandbox("json")
    try:
        port = sb.next_port()
        sb.write_server("s/app.py")
        sb.config(
            f"""
            version: 1
            services:
              api:
                kind: proc
                dir: ./s
                command: python3 app.py {port}
                port: {port}
                ready: {{ tcp: {port}, timeout: 20 }}
            """
        )
        run("up", cwd=sb.root)
        rc, out = run("status", "--json", cwd=sb.root)
        data = json.loads(out)
        check("valid json", isinstance(data, list) and data)
        check("reports running", data[0]["running"] is True)
        check("reports the port", data[0]["port"] == port)
        check("reports the log path", data[0]["log"].endswith("api.log"))
    finally:
        sb.cleanup()


def test_crash_is_reported_immediately():
    print("\n== a service that dies instantly must be reported at once, with its exit code ==")
    sb = Sandbox("crash")
    try:
        sb.config(
            """
            version: 1
            services:
              boom:
                kind: proc
                dir: ./s
                command: "echo about to fail; exit 3"
                ready: { cmd: "false", timeout: 300 }
              quiet-boom:
                kind: proc
                dir: ./s
                command: "exit 9"
                ready: { timeout: 300 }
            """
        )
        (sb.root / "s").mkdir(exist_ok=True)

        # Thresholds are deliberately loose: this asserts "it does not wait out the
        # 300s probe", not "it is fast". A machine running three dev servers can add
        # real latency to spawning a Python process, and a flaky test is worse than a
        # slow one.
        started = time.monotonic()
        rc, out = run("start", "boom", cwd=sb.root, timeout=180)
        elapsed = time.monotonic() - started
        check("a declared 300s probe does not stall on a crash", elapsed < 90, f"took {elapsed:.1f}s")
        check("the crash is reported", rc != 0 and "exited with code 3" in out, out[-300:])
        check("the real exit code is recorded", (sb.state("boom") or {}).get("exit_code") == 3, str(sb.state("boom")))

        rc, out = run("start", "quiet-boom", cwd=sb.root, timeout=60)
        check("a crash without a probe is not reported as success", rc != 0 and "exited with code 9" in out, out[-300:])
        check("its exit code is recorded too", (sb.state("quiet-boom") or {}).get("exit_code") == 9)
    finally:
        sb.cleanup()


def test_restart_does_not_bounce_dependencies():
    print("\n== restart <svc> must not restart the services it depends on ==")
    sb = Sandbox("deps")
    try:
        p_api, p_app = sb.next_port(), sb.next_port()
        sb.write_server("api/app.py")
        sb.write_server("app/app.py")
        sb.config(
            f"""
            version: 1
            services:
              db:
                kind: proc
                dir: ./api
                command: python3 app.py {p_api}
                port: {p_api}
                ready: {{ tcp: {p_api}, timeout: 25 }}
              app:
                kind: proc
                dir: ./app
                command: python3 app.py {p_app}
                port: {p_app}
                depends_on: [db]
                ready: {{ tcp: {p_app}, timeout: 25 }}
            """
        )
        rc, out = run("up", cwd=sb.root)
        check("stack starts", rc == 0, out[-300:])
        db_pid = sb.state("db")["pid"]
        app_pid = sb.state("app")["pid"]

        rc, out = run("restart", "app", cwd=sb.root)
        check("restart of the dependent succeeds", rc == 0, out[-300:])
        check("the dependent was restarted", sb.state("app")["pid"] != app_pid)
        check(
            "its dependency was NOT restarted (same pid)",
            sb.state("db")["pid"] == db_pid,
            f"{db_pid} -> {sb.state('db')['pid']}",
        )
        check("dependency still serving", port_busy(p_api))
    finally:
        sb.cleanup()


def test_cli_surface():
    print("\n== CLI surface: every subcommand parses, exec flags behave, init writes a valid config ==")
    sb = Sandbox("cli")
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("devpad", DEVCTL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for name in ("up", "down", "start", "stop", "restart", "status", "ps", "logs", "attach",
                     "exec", "ports", "kill", "rebuild", "doctor", "clean", "config", "init"):
            rc, out = run(name, "--help")
            check(f"`{name} --help` works", rc == 0, out[-150:])

        parser = mod.build_parser()
        for argv, want_tty, want_cmd in (
            (["exec", "infra", "mysql", "-uroot", "-p"], False, ["mysql", "-uroot", "-p"]),
            (["exec", "-t", "infra", "sh"], True, ["sh"]),
            (["exec", "infra", "psql", "-c", "select 1"], False, ["psql", "-c", "select 1"]),
            (["exec", "infra", "sh", "-c", "echo hi"], False, ["sh", "-c", "echo hi"]),
        ):
            parsed = parser.parse_args(mod.normalise_exec_argv(argv))
            check(
                f"exec parses {argv}",
                parsed.tty == want_tty and parsed.command == want_cmd and parsed.service == "infra",
                f"got tty={parsed.tty} cmd={parsed.command}",
            )

        rc, out = run("init", cwd=sb.root)
        check("init writes a starter config", rc == 0 and (sb.root / "devpad.yaml").is_file(), out[-200:])
        check("starter config is valid YAML for devpad", run("config", cwd=sb.root)[0] == 0)
        rc, out = run("init", cwd=sb.root)
        check("init refuses to overwrite", rc == 1 and "already exists" in out, out[-200:])
    finally:
        sb.cleanup()


def main() -> int:
    print(f"devpad tests  ({DEVCTL})")
    if not DEVCTL.is_file():
        print(f"cannot find {DEVCTL}")
        return 2
    for fn in (
        test_lifecycle,
        test_process_group_cleanup,
        test_port_guard_and_kill,
        test_stale_state_is_self_healing,
        test_config_validation,
        test_stdlib_only_and_discovery,
        test_optional_and_cmd_and_rebuild_plan,
        test_status_json,
        test_crash_is_reported_immediately,
        test_restart_does_not_bounce_dependencies,
        test_cli_surface,
    ):
        try:
            fn()
        except Exception as exc:  # a crashed test is a failed test
            FAIL.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  \033[31mERROR\033[0m {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
