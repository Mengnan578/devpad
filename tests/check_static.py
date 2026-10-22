#!/usr/bin/env python3
"""Static checks that catch the failure modes this codebase has actually hit.

Runner:  python3 tests/check_static.py

These are not style checks. Each rule exists because the bug it catches shipped at
least once during development:

  * an undefined name (a helper deleted during a refactor still being called)
  * a config the tool writes but cannot read back (YAML quoting)
  * an internal subcommand documented but unparseable
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
FILES = [
    SCRIPTS / "devpad.py",
    SCRIPTS / "terminals.py",
    SCRIPTS / "sysenv.py",
    SCRIPTS / "detect.py",
    HERE / "test_devpad.py",
    HERE / "test_terminals.py",
    HERE / "test_windows.py",
]

PASS: list[str] = []
FAIL: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(label if ok else f"{label} {detail}".strip())
    # Escape sequences are named constants: a backslash inside an f-string
    # expression is a syntax error before Python 3.12 (PEP 701), and CI runs 3.11.
    icon = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  {icon} {label} {'' if ok else detail}")


def undefined_calls(path: Path) -> list[tuple[str, int]]:
    """Names called but never bound anywhere in the module (a coarse pyflakes)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                defined.add(a.asname or a.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in defined:
                out.append((node.func.id, node.lineno))
    return out


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem + "_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_defined_names():
    print("\n== every called name is bound (catches refactor deletions) ==")
    for path in FILES:
        missing = undefined_calls(path)
        check(
            f"{path.name}: no undefined names",
            not missing,
            "; ".join(f"{n}() at line {l}" for n, l in missing),
        )


def test_yaml_roundtrip():
    print("\n== generated YAML round-trips through devpad's own loader ==")
    detect = load(SCRIPTS / "detect.py")
    devpad = load(SCRIPTS / "devpad.py")

    # Values chosen because each one broke the emitter at least once: glob stars
    # are YAML aliases, a leading dash is a sequence, `true` is a boolean.
    nasty = {
        "kind": "proc",
        "dir": "./s",
        "command": "python3 app.py --flag a:b # not a comment",
        "port": 8080,
        "ready": {"http": "http://127.0.0.1:8080/health", "timeout": 90},
        "clean_globs": ["*.egg-info", "__pycache__", "-weird", "true", "no", "1", "a: b", "*star", "&anchor"],
        "install": [".venv/bin/pip install -r requirements.txt"],
        "env": {"A": "1", "B": "true", "C": "with space", "D": "x#y"},
    }
    det = detect.Detection(Path("/tmp"))
    det.add(detect.Finding("api", "proc", nasty, "certain", "synthetic"))
    text = detect.render_yaml(det)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "devpad.yaml"
        cfg.write_text(text, encoding="utf-8")
        try:
            project = devpad.Project(cfg, devpad.load_config_file(cfg))
            svc = project.services["api"]
            ok = (
                svc.clean_globs == nasty["clean_globs"]
                and svc.env == nasty["env"]
                and svc.ready == ("http", nasty["ready"]["http"])
                and svc.resolved_command() == nasty["command"]
            )
            check("tricky values survive the round trip", ok, f"got {svc.clean_globs} / {svc.env}")
        except Exception as exc:
            check("tricky values survive the round trip", False, f"{type(exc).__name__}: {exc}")


def test_dual_stack_probe():
    """A server bound to one loopback family must still be seen as ready.

    Vite 7 binds `[::1]` only. Probing 127.0.0.1 alone made devpad wait out the
    full timeout and then report a healthy service as failed — the exact bug this
    test exists to prevent from coming back.
    """
    import http.server
    import socket
    import threading

    devpad = load(SCRIPTS / "devpad.py")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    class V6Server(http.server.HTTPServer):
        # address_family must be set before __init__ creates the socket; changing it
        # afterwards has no effect, which is why the naive version bound to IPv4.
        address_family = socket.AF_INET6

    servers = {}
    threads = []
    for cls, host, label in ((V6Server, "::1", "v6"), (http.server.HTTPServer, "127.0.0.1", "v4")):
        try:
            srv = cls((host, 0), Handler)
        except OSError:
            continue
        servers[label] = srv.socket.getsockname()[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        threads.append((t, srv))

    try:
        if "v6" in servers:
            port = servers["v6"]
            check("IPv6-only listener is detected via tcp", devpad.probe_once("tcp", port))
            check("IPv6-only listener is detected via port", devpad.probe_once("port", port))
            check(
                "an IPv4 URL still matches an IPv6-only server",
                devpad.probe_once("http", f"http://127.0.0.1:{port}/"),
                "http probe did not fall back to ::1",
            )
            check(
                "a localhost URL still matches an IPv6-only server",
                devpad.probe_once("http", f"http://localhost:{port}/"),
            )
        else:
            check("IPv6 loopback available on this machine", False, "skipped: no ::1")

        if "v4" in servers:
            port = servers["v4"]
            check("IPv4 listener is detected via tcp", devpad.probe_once("tcp", port))
            check(
                "an IPv6 URL still matches an IPv4-only server",
                devpad.probe_once("http", f"http://[::1]:{port}/"),
            )
        check("a closed port reports not-ready", not devpad.probe_once("tcp", 1))
    finally:
        for _, srv in threads:
            srv.shutdown()
            srv.server_close()


def test_cli_consistency():
    print("\n== the CLI parses, and --help lists every documented subcommand ==")
    rc = subprocess.run(
        [sys.executable, str(SCRIPTS / "devpad.py"), "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    check("devpad --help exits 0", rc.returncode == 0, rc.stdout[-200:])
    documented = [
        "up", "down", "start", "stop", "restart", "status", "ps", "logs",
        "attach", "exec", "ports", "kill", "rebuild", "doctor", "clean", "config", "init",
    ]
    for name in documented:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "devpad.py"), name, "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        check(f"`{name} --help` parses", r.returncode == 0, r.stdout[-120:])

    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "detect.py"), "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    check("detect.py --help exits 0", r.returncode == 0, r.stdout[-150:])


def test_detector_never_invents():
    print("\n== the detector refuses to invent services from nothing ==")
    detect = load(SCRIPTS / "detect.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "README.md").write_text("an empty project\n", encoding="utf-8")
        result = detect.detect(root)
        check("no services invented for an empty project", not result.findings)
        check("the emptiness is explained in a note", bool(result.notes))
        check("the draft is marked as a draft", "draft: true" in detect.render_yaml(result))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "package.json").write_text(
            json.dumps({"dependencies": {"react-native": "0.76.5"}}), encoding="utf-8")
        result = detect.detect(root)
        kinds = ", ".join(result.project_kinds)
        check(
            "a project with no start script yields a decision, not a command",
            not result.findings and bool(result.unknowns),
            f"kinds={kinds} services={[f.name for f in result.findings]}",
        )


def test_detector_fixtures():
    print("\n== detector output matches known project shapes ==")
    detect = load(SCRIPTS / "detect.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "mobile").mkdir()
        (root / "mobile" / "package.json").write_text(
            json.dumps({"scripts": {"start": "react-native start"}, "dependencies": {"react-native": "0.76.5"}}), encoding="utf-8")
        (root / "mobile" / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        (root / "server").mkdir()
        (root / "server" / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
        (root / "server" / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef h(): return {}\n", encoding="utf-8")
        (root / "infra").mkdir()
        (root / "infra" / "docker-compose.yml").write_text(
            'services:\n  db:\n    image: postgres:16\n    ports:\n      - "5432:5432"\n', encoding="utf-8")
        result = detect.detect(root)
        by_name = {f.name: f for f in result.findings}
        check("finds the compose infra", "infra" in by_name)
        check("infra declares the published host port", by_name.get("infra", None) and by_name["infra"].data.get("ready") == {"tcp": 5432, "timeout": 180})
        check("finds the fastapi backend", "api" in by_name)
        check(
            "backend readiness uses the discovered /health route",
            by_name.get("api", None) and by_name["api"].data["ready"].get("http", "").endswith("/health"),
        )
        check("uses the lockfile's package manager", by_name.get("app", None) and "pnpm" in by_name["app"].data["command"])
        check("respects the declared RN port", by_name.get("app", None) and by_name["app"].data["port"] == 8081)
        check("orders api after infra", by_name.get("api", None) and by_name["api"].data.get("depends_on") == ["infra"])
        check("orders app after api", by_name.get("app", None) and by_name["app"].data.get("depends_on") == ["api"])


def test_vite_uses_dev_script():
    print("\n== a Vite project gets `dev`, not `start` ==")
    detect = load(SCRIPTS / "detect.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}, "devDependencies": {"vite": "6"}}), encoding="utf-8")
        (root / "vite.config.ts").write_text("export default { server: { port: 4321 } }\n", encoding="utf-8")
        result = detect.detect(root)
        app = next((f for f in result.findings if f.name == "app"), None)
        check("uses the dev script", app is not None and app.data["command"].endswith(" dev"), app.data["command"] if app else "no app")
        check("honours the configured port", app is not None and app.data["port"] == 4321)


def main() -> int:
    print(f"static checks  ({ROOT})")
    for fn in (
        test_defined_names,
        test_yaml_roundtrip,
        test_dual_stack_probe,
        test_cli_consistency,
        test_detector_never_invents,
        test_detector_fixtures,
        test_vite_uses_dev_script,
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
