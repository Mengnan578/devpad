#!/usr/bin/env python3
"""Project reconnaissance for devctl.

Turns "what am I looking at" into a draft service config, so neither a human nor
an agent has to reconstruct long start commands from memory.

The contract this module keeps, and the reason it is trustworthy:

  * Everything reported as `certain` was read from a project file, never guessed.
  * Anything ambiguous is reported in `unknowns` with the alternatives found, so
    a human or an agent decides instead of the detector inventing a default.
  * Empty detection is a valid answer. A project with no package.json, no Python
    manifest and no compose file yields an empty draft plus a note, not a
    plausible-looking fiction.

Stdlib only. No network, no writes: detection is read-only reconnaissance.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# result containers
# ---------------------------------------------------------------------------


class Finding:
    """One detected service, plus how sure we are and why we think so."""

    def __init__(self, name: str, kind: str, data: dict, confidence: str, evidence: str):
        self.name = name
        self.kind = kind
        self.data = data
        self.confidence = confidence  # certain | likely | guess
        self.evidence = evidence

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "config": self.data,
        }


class Detection:
    def __init__(self, root: Path):
        self.root = root
        self.findings: list[Finding] = []
        self.unknowns: list[dict] = []
        self.notes: list[str] = []
        self.project_kinds: list[str] = []

    def add(self, finding: Finding):
        self.findings.append(finding)

    def unknown(self, question: str, options: list[str] | None = None, service: str | None = None):
        self.unknowns.append({"service": service, "question": question, "options": options or []})

    def note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "project_kinds": self.project_kinds,
            "services": [f.to_dict() for f in self.findings],
            "unknowns": self.unknowns,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first_existing(root: Path, names: list[str], extra_dirs: list[str] | None = None) -> Path | None:
    search = [root] + [root / d for d in (extra_dirs or [])]
    for base in search:
        for name in names:
            cand = base / name
            if cand.exists():
                return cand
    return None


def _is_dir_with(root: Path, rel: str, must_contain: list[str]) -> bool:
    d = root / rel
    if not d.is_dir():
        return False
    return all((d / m).exists() for m in must_contain)


# ---------------------------------------------------------------------------
# frontend: React Native / Expo / web bundlers
# ---------------------------------------------------------------------------


def detect_frontend(root: Path, det: Detection) -> Finding | None:
    pkg_path = _first_existing(root, ["package.json"], extra_dirs=["mobile", "app", "frontend", "client"])
    if not pkg_path:
        return None
    base = pkg_path.parent
    pkg = _read_json(pkg_path) or {}
    deps = {}
    for key in ("dependencies", "devDependencies"):
        section = pkg.get(key)
        if isinstance(section, dict):
            deps.update(section)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}

    is_expo = "expo" in deps
    is_rn = "react-native" in deps
    is_vite = "vite" in deps
    is_next = "next" in deps

    if not (is_expo or is_rn or is_vite or is_next):
        det.note(f"{pkg_path.relative_to(root)} exists but names no known bundler; skipped")
        return None

    if is_expo:
        framework = "expo"
        default_script = "start"
        port = 8081
    elif is_rn:
        framework = "react-native"
        default_script = "start"
        port = 8081
    elif is_next:
        framework = "next"
        default_script = "dev"
        port = 3000
    else:
        framework = "vite"
        default_script = "dev"
        port = 5173

    manager = _detect_package_manager(base, det)
    script_name = _pick_script(scripts, default_script)
    if script_name is None:
        det.unknown(
            f"{framework} bundle script not found in package.json scripts: "
            f"{sorted(scripts) or 'none'} — how do you normally start the dev server?",
            options=[f"{manager} run <script>"] + [f"{manager} run {s}" for s in sorted(scripts)],
            service="app",
        )
        return None

    if script_name != default_script:
        det.note(
            f"{framework} usually starts with the `{default_script}` script, but this package.json "
            f"defines `{script_name}`; using that"
        )

    # `pnpm start` is unambiguous; a bare `npm start` is an alias for `npm run start`.
    command = f"{manager} run {script_name}" if manager != "npm" else f"npm run {script_name}"

    # Honour an explicitly declared port where the framework supports one.
    port_evidence = f"framework default for {framework}"
    override = _detect_js_port(base, framework)
    if override:
        port, port_evidence = override, "declared in project config"

    ready_port = 8081 if framework in ("expo", "react-native") else port
    if framework in ("expo", "react-native"):
        port = ready_port

    data = {
        "kind": "proc",
        "description": f"{framework} bundler",
        "dir": _rel(root, base),
        "command": command,
        "port": port,
        "ready": {"tcp": ready_port, "timeout": 180},
        "install": _js_install_command(manager, base),
        "clean_globs": ["node_modules/.cache"],
    }
    evidence = (
        f"{pkg_path.relative_to(root)} declares {framework}; "
        f"script `{script_name}` = {scripts.get(script_name)!r}; port {port} ({port_evidence})"
    )
    det.project_kinds.append(framework)
    return Finding("app", "proc", data, "certain", evidence)


def _pick_script(scripts: dict, preferred: str) -> str | None:
    """Choose the dev-server script, preferring the framework's own convention.

    Vite and Next define `dev`; React Native and Expo define `start`. Guessing
    wrong yields a config that fails on first run, so an unknown name is reported
    to the caller rather than invented.
    """
    for name in (preferred, "dev", "start", "serve", "develop", "start:dev"):
        if name in scripts:
            return name
    return None


def _detect_package_manager(base: Path, det: Detection) -> str:
    """Read the lockfile. A lockfile is a fact; a preference is not."""
    if (base / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (base / "yarn.lock").exists():
        return "yarn"
    if (base / "package-lock.json").exists():
        return "npm"
    if (base / "bun.lockb").exists() or (base / "bun.lock").exists():
        return "bun"
    # No lockfile: fall back to what is installed, and say so later.
    for mgr in ("pnpm", "yarn", "npm"):
        if shutil.which(mgr):
            det.note(f"no lockfile in {base.name or '.'}; assuming {mgr} (first package manager on PATH)")
            return mgr
    det.note(f"no lockfile and no package manager found for {base.name or '.'}; assuming npm")
    return "npm"


def _js_install_command(manager: str, base: Path) -> list[str]:
    if manager == "pnpm":
        return ["pnpm install"]
    if manager == "yarn":
        return ["yarn install"]
    if manager == "bun":
        return ["bun install"]
    if (base / "package-lock.json").exists():
        return ["npm ci"]
    return ["npm install"]


def _detect_js_port(base: Path, framework: str) -> int | None:
    """Look for an explicitly configured dev-server port."""
    if framework == "vite":
        for name in ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs"):
            text = _read_text(base / name)
            m = re.search(r"port\s*:\s*(\d{2,5})", text)
            if m:
                return int(m.group(1))
    if framework in ("react-native", "expo"):
        # Metro's port can be pinned in metro.config.js or react-native.config.js
        for name in ("metro.config.js", "metro.config.ts"):
            text = _read_text(base / name)
            m = re.search(r"port\s*[:=]\s*(\d{2,5})", text)
            if m:
                return int(m.group(1))
        # Expo reads RCT_METRO_PORT / --port from package.json scripts
        pkg = _read_json(base / "package.json") or {}
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        for value in scripts.values():
            if isinstance(value, str):
                m = re.search(r"--port[= ](\d{2,5})", value)
                if m:
                    return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# backend: Python
# ---------------------------------------------------------------------------


PY_MANIFESTS = ["pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "poetry.lock", "uv.lock"]


def detect_backend(root: Path, det: Detection) -> Finding | None:
    base = None
    manifest = None
    for candidate in [root, *[root / d for d in ("server", "backend", "api", "src")]]:
        found = _first_existing(candidate, PY_MANIFESTS)
        if found:
            base, manifest = candidate, found
            break
    # A bare app.py/manage.py with no manifest is still a Python project.
    if base is None:
        for candidate in [root, *[root / d for d in ("server", "backend", "api")]]:
            if _first_existing(candidate, ["manage.py", "main.py", "app.py", "asgi.py", "wsgi.py"]):
                base, manifest = candidate, None
                break
    if base is None:
        return None

    py_text = _read_text(manifest) if manifest else ""
    has_django = (base / "manage.py").exists()
    has_fastapi = bool(re.search(r"\bfastapi\b", py_text, re.I)) or _imports_any(base, ["fastapi"])
    has_flask = bool(re.search(r"\bflask\b", py_text, re.I)) or _imports_any(base, ["flask"])
    has_uvicorn = bool(re.search(r"\buvicorn\b", py_text, re.I))

    runner = _python_runner(base, det)
    install = _python_install_command(base, manifest, runner, det)

    if has_django:
        port = _django_port(base) or 8000
        data = {
            "kind": "proc",
            "description": "django backend",
            "dir": _rel(root, base),
            "command": f"{runner} manage.py runserver 0.0.0.0:{port}",
            "port": port,
            "ready": {"tcp": port, "timeout": 90},
            "install": install,
            "clean_globs": ["__pycache__", ".pytest_cache", ".mypy_cache", "*.egg-info"],
        }
        det.project_kinds.append("django")
        return Finding(
            "api",
            "proc",
            data,
            "certain",
            f"manage.py at {_rel(root, base)}/{'manage.py'}",
        )

    if has_fastapi or has_uvicorn:
        found_target = _detect_asgi_target(base, det)
        port = 8000
        if found_target is None:
            det.unknown(
                "could not determine the ASGI application path for uvicorn "
                "(no `app = FastAPI()` found in main.py / app/main.py / src/**)",
                options=["main:app", "app.main:app", "src.main:app"],
                service="api",
            )
            return None
        target, target_file = found_target
        health = _detect_health_path(base, target_file)
        if health:
            ready = {"http": f"http://127.0.0.1:{port}{health}", "timeout": 90}
            det.note(f"readiness probe upgraded to HTTP because {health} exists in the source")
        else:
            ready = {"tcp": port, "timeout": 90}
            det.note(
                "no /health endpoint found, so readiness falls back to a TCP probe; "
                "adding one gives a stronger signal"
            )
        candidates = _fastapi_alternatives(base)
        data = {
            "kind": "proc",
            "description": "fastapi backend",
            "dir": _rel(root, base),
            "command": f"{runner} -m uvicorn {target} --reload --host 0.0.0.0 --port {port}",
            "port": port,
            "ready": ready,
            "install": install,
            "clean_globs": ["__pycache__", ".pytest_cache", ".mypy_cache", "*.egg-info"],
        }
        if candidates:
            det.note(f"other ASGI candidates found: {', '.join(candidates)}")
        det.project_kinds.append("fastapi")
        return Finding("api", "proc", data, "likely", f"fastapi/uvicorn in {_rel(root, manifest or base)}")

    if has_flask:
        port = 5000
        det.unknown(
            "flask app detected but the app/module path cannot be inferred reliably "
            "(factory pattern or unusual layout) — confirm the start command",
            options=[f"{runner} -m flask --app app run --debug --port {port}",
                     f"{runner} -m flask --app main run --debug --port {port}"],
            service="api",
        )
        data = {
            "kind": "proc",
            "description": "flask backend (CONFIRM THE COMMAND)",
            "dir": _rel(root, base),
            "command": f"{runner} -m flask --app app run --debug --port {port}",
            "port": port,
            "ready": {"tcp": port, "timeout": 90},
            "install": install,
            "clean_globs": ["__pycache__", ".pytest_cache"],
        }
        det.project_kinds.append("flask")
        return Finding("api", "proc", data, "guess", f"flask in {_rel(root, manifest or base)}")

    det.unknown(
        f"python project at {_rel(root, base)} but no web framework was identified — "
        "what command starts it?",
        service="api",
    )
    return None


def _imports_any(base: Path, names: list[str]) -> bool:
    """Shallow scan of top-level .py files for an import of one of `names`."""
    pattern = re.compile(r"^\s*(?:from|import)\s+(" + "|".join(re.escape(n) for n in names) + r")\b", re.M | re.I)
    for path in list(base.glob("*.py"))[:40]:
        if pattern.search(_read_text(path)):
            return True
    for sub in ("app", "src", "core"):
        for path in list((base / sub).glob("*.py"))[:40] if (base / sub).is_dir() else []:
            if pattern.search(_read_text(path)):
                return True
    return False


VENV_PREFIX = (".venv/bin/", "../.venv/bin/", ".venv/Scripts/", "../.venv/Scripts/")


def _venv_prefix(base: Path) -> str | None:
    """Relative prefix of an existing virtualenv's bin/Scripts dir, or None."""
    for prefix in VENV_PREFIX:
        if (base / prefix / "python").exists() or (base / prefix / "python.exe").exists():
            return prefix
    return None


def _python_runner(base: Path, det: Detection) -> str:
    """The interpreter that will both install and run — one decision, one prefix.

    Using one prefix for `install` and another for `command` is how a project ends
    up with packages in a venv nobody runs. Poetry and uv are reported as decisions
    when their CLI is absent rather than silently downgraded.
    """
    prefix = _venv_prefix(base)
    if prefix:
        return f"./{prefix}python" if not prefix.startswith("..") else f"{prefix}python"

    if (base / "poetry.lock").exists() or re.search(r"\[tool\.poetry\]", _read_text(base / "pyproject.toml")):
        if shutil.which("poetry"):
            return "poetry run python"
        det.unknown(
            "pyproject.toml uses Poetry but `poetry` is not on PATH — install Poetry, or "
            "switch the project to a plain venv and set the command/install lines accordingly",
            options=["pip install poetry && poetry install", "python3 -m venv .venv && .venv/bin/pip install -e ."],
            service="api",
        )
        return "./.venv/bin/python"

    if (base / "uv.lock").exists():
        if shutil.which("uv"):
            return "uv run python"
        det.unknown(
            "uv.lock exists but `uv` is not on PATH — install uv, or use a plain venv",
            options=["pip install uv && uv sync", "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"],
            service="api",
        )
        return "./.venv/bin/python"

    return "./.venv/bin/python"


def _python_install_command(base: Path, manifest: Path | None, runner: str, det: Detection) -> list[str]:
    if (base / "poetry.lock").exists() and shutil.which("poetry"):
        return ["poetry install"]
    if (base / "uv.lock").exists() and shutil.which("uv"):
        return ["uv sync"]
    if (base / "Pipfile").exists() and shutil.which("pipenv"):
        return ["pipenv install"]

    # One decision for both install and run: never install into a venv that is not
    # the one `command` uses, and never fall back to the system interpreter.
    prefix = _venv_prefix(base) or ".venv/bin/"
    if prefix.startswith(".."):
        prefix = prefix[3:] if prefix.startswith("../") else prefix

    if manifest and manifest.name == "requirements.txt":
        if _venv_prefix(base):
            return [f"{prefix}pip install -r requirements.txt"]
        return ["python3 -m venv .venv", ".venv/bin/pip install -r requirements.txt"]
    if manifest and manifest.name == "pyproject.toml":
        if _venv_prefix(base):
            return [f"{prefix}pip install -e ."]
        return ["python3 -m venv .venv", ".venv/bin/pip install -e ."]
    if (base / "requirements.txt").exists():
        if _venv_prefix(base):
            return [f"{prefix}pip install -r requirements.txt"]
        return ["python3 -m venv .venv", ".venv/bin/pip install -r requirements.txt"]
    det.note(f"install step for {_rel(base.parent, base)} not determined; edit the generated `install` list")
    return []


def _detect_health_path(base: Path, target_file: Path | None) -> str | None:
    """Find a real liveness endpoint in the source, so readiness is not a bare port.

    Only route decorators count: a string that merely contains 'health' is not
    evidence that an endpoint answers.
    """
    pattern = re.compile(
        r"""get\s*\(\s*['"](/[A-Za-z0-9_\-/]*(?:health|healthz|ping|ready|live)[A-Za-z0-9_\-/]*)['"]""",
        re.I,
    )
    candidates: list[Path] = []
    if target_file and target_file.is_file():
        candidates.append(target_file)
    candidates += sorted(base.glob("*.py"))[:20]
    for sub in ("app", "api", "src", "server"):
        d = base / sub
        if d.is_dir():
            candidates += sorted(d.glob("*.py"))[:30]
            for routes in ("routers", "routes", "api"):
                rd = d / routes
                if rd.is_dir():
                    candidates += sorted(rd.glob("*.py"))[:30]
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        m = pattern.search(_read_text(path))
        if m:
            return m.group(1)
    return None


def _detect_asgi_target(base: Path, det: Detection) -> tuple[str, Path | None] | None:
    """Find `<module>:<attr>` for the FastAPI/ASGI app by looking for the assignment."""
    assignment = re.compile(r"^\s*(\w+)\s*(?::[^=]+)?=\s*(?:FastAPI|Starlette)\s*\(", re.M)
    for rel in ("main.py", "app/main.py", "src/main.py", "app.py", "api/main.py", "server/main.py"):
        path = base / rel
        if not path.is_file():
            continue
        m = assignment.search(_read_text(path))
        attr = m.group(1) if m else None
        if not attr:
            # uvicorn-style `app = app` re-export or a factory; only trust a plain name
            m2 = re.search(r"^\s*(\w+)\s*=\s*create_app\s*\(", _read_text(path), re.M)
            attr = m2.group(1) if m2 else "app"
        module = rel[:-3].replace("/", ".")
        return f"{module}:{attr}", path
    # src/<pkg>/main.py layout
    for path in sorted(base.glob("src/*/main.py")) + sorted(base.glob("*/main.py")):
        rel = path.relative_to(base)
        text = _read_text(path)
        m = assignment.search(text)
        attr = m.group(1) if m else None
        if attr:
            return f"{rel.with_suffix('').as_posix().replace('/', '.')}:{attr}", path
    return None


def _fastapi_alternatives(base: Path) -> list[str]:
    found = []
    for path in list(base.glob("*.py")) + list(base.glob("app/*.py")) + list(base.glob("src/**/*.py"))[:20]:
        if re.search(r"\bFastAPI\s*\(", _read_text(path)):
            rel = path.relative_to(base).with_suffix("").as_posix().replace("/", ".")
            found.append(f"{rel}:app")
    return found[:5]


def _django_port(base: Path) -> int | None:
    for settings in base.glob("**/settings.py"):
        m = re.search(r"runserver\s+[\d.]+:(\d+)", _read_text(settings))
        if m:
            return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# infrastructure: docker compose
# ---------------------------------------------------------------------------


COMPOSE_NAMES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "docker-compose.dev.yml",
    "docker-compose.dev.yaml",
    "compose.dev.yml",
    "compose.dev.yaml",
]


def find_compose_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for base in [root, *[root / d for d in ("infra", "deploy", "docker", "ops", ".devcontainer")]]:
        for name in COMPOSE_NAMES:
            cand = base / name
            if cand.is_file() and cand not in found:
                found.append(cand)
    return found


def detect_compose(root: Path, det: Detection) -> Finding | None:
    files = find_compose_files(root)
    if not files:
        return None
    if len(files) > 1:
        det.unknown(
            "several compose files found — which one holds the databases you need? "
            "(or should they be separate services?)",
            options=[_rel(root, f) for f in files],
            service="infra",
        )
    chosen = files[0]
    text = _read_text(chosen)
    names = _compose_service_names(text)
    host_ports = _compose_host_ports(text)

    if not names:
        det.unknown(
            f"{_rel(root, chosen)} was found but its services could not be parsed — "
            "which compose services should devctl manage?",
            service="infra",
        )
        return None

    # Only the data-ish services are interesting for readiness; app containers are
    # usually replaced by a local process during development.
    data_names = [n for n in names if re.search(r"db|sql|postgres|mysql|maria|mongo|redis|cache|queue|kafka|rabbit", n, re.I)]
    managed = data_names or names

    ready = None
    for candidate in managed:
        port = host_ports.get(candidate)
        if port:
            ready = {"tcp": port, "timeout": 180}
            break
    if ready is None and host_ports:
        ready = {"tcp": next(iter(host_ports.values())), "timeout": 180}
        det.note("readiness probe uses a host port published by the compose file; verify it is reachable")
    if ready is None:
        det.unknown(
            f"no published host port found in {_rel(root, chosen)}; a TCP readiness probe cannot be built — "
            "is a port published, or should a `cmd` probe such as `docker compose exec -T <svc> pg_isready` be used?",
            service="infra",
        )

    data = {
        "kind": "docker",
        "description": "database / middleware containers",
        "dir": _rel(root, chosen.parent),
        "compose": chosen.name,
        "services": managed,
    }
    if ready:
        data["ready"] = ready
    elif host_ports:
        data["ready"] = {"tcp": next(iter(host_ports.values())), "timeout": 180}

    evidence = (
        f"{_rel(root, chosen)} declares {len(names)} service(s): {', '.join(names)}; "
        f"managing {', '.join(managed)}"
        + (f"; host ports {host_ports}" if host_ports else "; no published host ports found")
    )
    det.project_kinds.append("docker-compose")
    if not data_names and len(names) > 1:
        det.note("no obvious database service in the compose file; all services are managed")
    return Finding("infra", "docker", data, "certain" if ready else "likely", evidence)


def _compose_service_names(text: str) -> list[str]:
    """Service names from the top-level `services:` block, without a YAML library."""
    names: list[str] = []
    lines = text.split("\n")
    in_services = False
    services_indent = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if not in_services:
            if re.match(r"^services\s*:", stripped):
                in_services = True
                services_indent = indent
                # inline form: services: {a: ..., b: ...}
                inline = stripped.split(":", 1)[1].strip()
                if inline.startswith("{"):
                    for part in inline.strip("{}").split(","):
                        key = part.split(":", 1)[0].strip().strip("'\"")
                        if key:
                            names.append(key)
            continue
        if indent <= services_indent:
            break
        if indent == services_indent + 2 or (services_indent is not None and indent == services_indent + 1):
            m = re.match(r"^([A-Za-z0-9._-]+)\s*:", stripped)
            if m:
                names.append(m.group(1))
    return names


def _compose_host_ports(text: str) -> dict[str, int]:
    """Map service name -> first published host port, from short and long syntax."""
    ports: dict[str, int] = {}
    lines = text.split("\n")
    current = None
    in_services = False
    services_indent = None
    pending_list = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if not in_services:
            if re.match(r"^services\s*:", stripped):
                in_services = True
                services_indent = indent
            continue
        if indent <= services_indent:
            break
        if indent == services_indent + 2:
            m = re.match(r"^([A-Za-z0-9._-]+)\s*:", stripped)
            if m:
                current = m.group(1)
                pending_list = False
            continue
        if current is None:
            continue
        if re.match(r"^ports\s*:", stripped):
            inline = stripped.split(":", 1)[1].strip()
            pending_list = inline == ""
            if inline.startswith("["):
                for item in inline.strip("[]").split(","):
                    port = _host_port_from_mapping(item.strip().strip("'\""))
                    if port:
                        ports.setdefault(current, port)
            elif inline:
                port = _host_port_from_mapping(inline.strip("'\""))
                if port:
                    ports.setdefault(current, port)
            continue
        if pending_list and stripped.startswith("-"):
            port = _host_port_from_mapping(stripped[1:].strip().strip("'\""))
            if port:
                ports.setdefault(current, port)
            continue
        if pending_list and not stripped.startswith("-"):
            pending_list = False
    return ports


def _host_port_from_mapping(item: str) -> int | None:
    """`8080:80`, `127.0.0.1:5432:5432`, `3306`, `"6379:6379"` -> host port."""
    if not item:
        return None
    item = item.strip().strip("'\"")
    if item.startswith("{"):
        # long syntax "{ target: 80, published: 8080 }"
        m = re.search(r"published\s*:\s*['\"]?(\d+)", item)
        return int(m.group(1)) if m else None
    parts = item.split(":")
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0])
    if len(parts) >= 2:
        candidate = parts[-2]
        if candidate.isdigit():
            return int(candidate)
    return None


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def _rel(root: Path, path: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    text = rel.as_posix()
    return "./" + text if text != "." else "."


def detect(root: Path) -> Detection:
    det = Detection(root)
    frontend = detect_frontend(root, det)
    backend = detect_backend(root, det)
    infra = detect_compose(root, det)

    # Dependency order: infra -> api -> app, unless a service already declares otherwise.
    for finding in (infra, backend, frontend):
        if finding is None:
            continue
        if finding.name == "api" and infra is not None:
            finding.data["depends_on"] = ["infra"]
        if finding.name == "app" and backend is not None:
            finding.data["depends_on"] = ["api"]
        det.add(finding)

    if not det.findings:
        det.note(
            "no runnable service could be derived here. "
            + (
                f"Recognised: {', '.join(dict.fromkeys(det.project_kinds))}, but the start command "
                "could not be determined — see NEEDS A DECISION above and fill in the placeholder below."
                if det.project_kinds
                else "No package.json with a bundler, no Python web framework and no compose file were found. "
                "Point devctl at the right directory, or write the services by hand."
            )
        )
    return det


def render_yaml(det: Detection) -> str:
    """Emit a devctl.yaml. Hand-written rather than dumped, so comments survive."""
    # Only mark the file as a draft when something still needs a human: a fully
    # certain detection should be runnable immediately without editing.
    is_draft = bool(det.unknowns) or not det.findings
    lines = [
        "# Generated by `devctl init --detect`.",
        "#",
        "# Values were read from files in this project. Anything marked LIKELY or GUESS",
        "# was inferred rather than read — check those before trusting them.",
        "# Re-run `devctl doctor` after editing.",
    ]
    if is_draft:
        lines += [
            "#",
            "# `draft: true` is here because something still needs a decision (see the",
            "# comments at the bottom). Delete it once this config is yours.",
        ]
    lines += [
        "",
        "version: 1",
    ]
    if is_draft:
        lines.append("draft: true")
    lines += [
        "",
        "defaults:",
        "  stop_timeout: 10",
        "",
        "services:",
    ]
    if not det.findings:
        lines.append("  # No service could be derived from this project. Fill one in:")
        lines.append("  #")
        for u in det.unknowns:
            lines.append(f"  # DECISION [{u.get('service') or '?'}]: {u['question']}")
            for opt in u.get("options", []):
                lines.append(f"  #   option: {opt}")
        lines.append("  # api:")
        lines.append("  #   kind: proc")
        lines.append("  #   dir: ./server")
        lines.append('  #   command: "<your start command>"')
        lines.append("  #   port: 8000")
        lines.append("  #   ready: {http: \"http://127.0.0.1:8000/health\", timeout: 90}")
        return "\n".join(lines) + "\n"

    for finding in det.findings:
        marker = "" if finding.confidence == "certain" else f"   # {finding.confidence.upper()} — verify"
        lines.append("")
        lines.append(f"  # {finding.evidence}")
        lines.append(f"  {finding.name}:{marker}")
        for key, value in finding.data.items():
            lines.append(f"    {key}: {_yaml_value(value)}")
    lines.append("")
    return "\n".join(lines)


# Characters that make a plain YAML scalar invalid or ambiguous at its start:
# `*` and `&` are anchors/aliases, `!` a tag, `%` a directive, `@`/`` ` `` reserved.
_YAML_UNSAFE_START = set("*&!|>%@`\"'#,[]{}?-: ")
_YAML_UNSAFE_ANY = set(":#[]{},")


def _yaml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_yaml_value(v)}" for k, v in value.items()) + "}"
    text = str(value)
    needs_quote = (
        text == ""
        or text != text.strip()
        or text[0] in _YAML_UNSAFE_START
        or any(ch in _YAML_UNSAFE_ANY for ch in text)
        or ": " in text
        or " #" in text
        or text.lower() in ("true", "false", "null", "~", "yes", "no", "on", "off")
        or re.fullmatch(r"[-+]?\d+(\.\d+)?", text) is not None
    )
    if not needs_quote:
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def render_report(det: Detection) -> str:
    out = [f"devctl detect — {det.root}", ""]
    if det.project_kinds:
        out.append(f"detected: {', '.join(dict.fromkeys(det.project_kinds))}")
    else:
        out.append("detected: nothing")
    out.append("")
    if det.findings:
        out.append("SERVICES")
        for f in det.findings:
            icon = {"certain": "ok  ", "likely": "maybe", "guess": "GUESS"}[f.confidence]
            out.append(f"  [{icon}] {f.name:6} ({f.kind})")
            out.append(f"          {f.evidence}")
            if "command" in f.data:
                out.append(f"          command: {f.data['command']}")
            if "compose" in f.data:
                out.append(f"          compose: {f.data['dir']}/{f.data['compose']} -> {f.data.get('services')}")
            if "ready" in f.data:
                out.append(f"          ready:   {f.data['ready']}")
            out.append("")
    if det.unknowns:
        out.append("NEEDS A DECISION")
        for u in det.unknowns:
            where = f"[{u['service']}] " if u.get("service") else ""
            out.append(f"  ? {where}{u['question']}")
            for opt in u.get("options", []):
                out.append(f"      - {opt}")
        out.append("")
    if det.notes:
        out.append("NOTES")
        for n in det.notes:
            out.append(f"  - {n}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="detect.py",
        description="Inspect a project and draft a devctl service config from what is actually there.",
    )
    p.add_argument("root", nargs="?", default=".", help="project root to inspect (default: cwd)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--write", metavar="PATH", help="write the generated YAML to PATH")
    p.add_argument("--print", dest="show", action="store_true", help="print the generated YAML")
    args = p.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"detect: not a directory: {root}")
        return 2

    det = detect(root)

    if args.json:
        payload = det.to_dict()
        if args.write or args.show:
            payload["yaml"] = render_yaml(det)
        print(json.dumps(payload, indent=2))
    else:
        print(render_report(det))
        if args.show:
            print("GENERATED CONFIG")
            print(render_yaml(det))

    if args.write:
        target = Path(args.write)
        if not target.is_absolute():
            target = root / target
        if target.exists():
            print(f"detect: refusing to overwrite {target} (move it aside first)")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_yaml(det), encoding="utf-8")
        print(f"wrote {target}")

    # Exit non-zero whenever a human decision is still required, or nothing was
    # found at all, so a caller can branch on "the draft is complete and usable".
    return 0 if (det.findings and not det.unknowns) else 1


if __name__ == "__main__":
    sys.exit(main())
