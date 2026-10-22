#!/usr/bin/env python3
"""devpad - a small, dependency-free local dev orchestrator.

Manages three kinds of services from one declaration:

  proc    a local foreground-ish process (python backend, react-native metro, ...)
  docker  a set of containers driven by `docker compose`
  cmd     a one-shot command (dependency install, image build, cache wipe)

Design goals, in the order they matter:
  1. Restarting one thing must never require restarting everything else.
  2. A crashed/leftover process must be visible and removable, not mysterious.
  3. Output must be readable in real time, by a human or by an agent.
  4. Zero third-party dependencies: stdlib only, so it runs on any machine.

Config is discovered from the nearest ancestor holding devpad.yaml / .devpad/config.yaml,
so the same command works from any subdirectory of the project.
"""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

VERSION = "0.1.0"

CONFIG_NAMES = (
    "devpad.yaml", "devpad.yml", ".devpad/config.yaml", ".devpad/config.yml", "devpad.json",
    # accepted so an existing devpad setup keeps working
    "devctl.yaml", "devctl.yml", ".devctl/config.yaml", ".devctl/config.yml", "devctl.json",
)

# ---------------------------------------------------------------------------
# tiny YAML subset loader (PyYAML is used when importable)
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    pass


def _scalar(tok: str):
    if tok == "" or tok == "~" or tok.lower() in ("null", "none"):
        return None
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "'\"":
        body = tok[1:-1]
        return body.replace("\\n", "\n") if tok[0] == '"' else body
    low = tok.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[-+]?\d+", tok):
        return int(tok)
    if re.fullmatch(r"[-+]?\d*\.\d+([eE][-+]?\d+)?", tok):
        return float(tok)
    return tok


def _split_inline(text: str) -> list[str]:
    out, buf, quote, depth = [], [], None, 0
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [p.strip() for p in out if p.strip() != ""]


def _inline_value(text: str):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [_inline_value(p) for p in _split_inline(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        obj = {}
        for part in _split_inline(text[1:-1]):
            if ":" not in part:
                raise ConfigError(f"bad inline map entry: {part!r}")
            k, v = part.split(":", 1)
            obj[str(_scalar(k.strip()))] = _inline_value(v)
        return obj
    return _scalar(text)


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _mini_yaml(text: str, source: str):
    """Parse the YAML subset devpad configs use: nested maps, `- ` lists,
    inline lists/maps, quoted scalars, comments, block scalars (| and >)."""
    root: dict = {}
    # stack of (indent, container)
    stack: list[tuple[int, object]] = [(-1, root)]
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if "\t" in stripped[:indent]:
            raise ConfigError(f"{source}: tabs are not allowed for indentation (line {i})")
        body = stripped.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if body.startswith("- "):
            item_text = body[2:].strip()
            if not isinstance(parent, list):
                raise ConfigError(f"{source}: list item without a list parent (line {i})")
            if item_text == "":
                child: object = {}
                parent.append(child)
                stack.append((indent, child))
                continue
            if ":" in item_text and not item_text.startswith(("[", "{", "'", '"')):
                k, v = item_text.split(":", 1)
                child = {str(_scalar(k.strip())): _inline_value(v) if v.strip() else None}
                parent.append(child)
                stack.append((indent, child))
                if not v.strip():
                    # value may be a nested block on following lines
                    stack[-1] = (indent, _placeholder_for(child, str(_scalar(k.strip()))))
                continue
            parent.append(_inline_value(item_text))
            continue

        if body == "-":
            if not isinstance(parent, list):
                raise ConfigError(f"{source}: bare list item without a list parent (line {i})")
            parent.append(None)
            continue

        if ":" not in body:
            raise ConfigError(f"{source}: expected `key: value` (line {i}): {body!r}")
        key, _, value = body.partition(":")
        key = str(_scalar(key.strip()))
        value = value.strip()

        if value in ("|", "|-", ">", ">-"):
            block: list[str] = []
            base_indent = None
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "":
                    block.append("")
                    i += 1
                    continue
                ni = len(nxt) - len(nxt.lstrip(" "))
                if ni <= indent:
                    break
                if base_indent is None:
                    base_indent = ni
                block.append(nxt[base_indent:])
                i += 1
            while block and block[-1] == "":
                block.pop()
            joined = "\n".join(block)
            if value.startswith(">"):
                joined = " ".join(p.strip() for p in joined.split("\n") if p.strip())
            parent[key] = joined
            continue

        if value == "":
            # decide map vs list by peeking at the next meaningful line
            nxt_container: object = {}
            j = i
            while j < len(lines):
                probe = _strip_comment(lines[j])
                if probe.strip():
                    pindent = len(probe) - len(probe.lstrip(" "))
                    if pindent > indent and probe.strip().startswith("-"):
                        nxt_container = []
                    break
                j += 1
            parent[key] = nxt_container
            stack.append((indent, nxt_container))
            continue

        parent[key] = _inline_value(value)
    return root


def _placeholder_for(child: dict, key: str):
    """After `- key:` we may discover a nested block; replace the None with it lazily."""
    # We cannot know yet; use a sentinel dict that the parser fills in place.
    child[key] = {}
    return child


def load_config_file(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        return data if data is not None else {}
    except ImportError:
        return _mini_yaml(text, str(path))


def find_config(start: Path | None = None) -> Path:
    env = os.environ.get("DEVPAD_CONFIG") or os.environ.get("DEVCTL_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise ConfigError(f"DEVPAD_CONFIG points at a missing file: {p}")
        return p
    cur = (start or Path.cwd()).resolve()
    for base in [cur, *cur.parents]:
        for name in CONFIG_NAMES:
            cand = base / name
            if cand.is_file():
                return cand
    raise ConfigError(
        "no devpad config found. Looked for "
        + ", ".join(CONFIG_NAMES)
        + f" from {cur} upwards. Run `devpad init` to generate one."
    )


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


DEFAULT_PORTS = {
    "metro": 8081,
    "server": 8080,
    "api": 8080,
    "web": 5173,
    "vite": 5173,
    "db": 3306,
    "mysql": 3306,
    "postgres": 5432,
    "redis": 6379,
    "mongo": 27017,
}


class Service:
    def __init__(self, name: str, raw: dict, project: "Project"):
        if not isinstance(raw, dict):
            raise ConfigError(f"service {name!r} must be a mapping")
        self.name = name
        self.raw = raw
        self.project = project
        self.kind = str(raw.get("kind", "proc")).lower()
        if self.kind not in ("proc", "docker", "cmd"):
            raise ConfigError(f"service {name!r}: unknown kind {self.kind!r}")

        # `run:` is sugar: a docker image/command one-shot
        if self.kind == "cmd" and "image" in raw and "command" not in raw and "cmd" not in raw:
            raise ConfigError(f"service {name!r}: kind cmd needs `command`")

        self.dir = Path(raw.get("dir", ".")).expanduser()
        if not self.dir.is_absolute():
            self.dir = (project.root / self.dir).resolve()
        self.command = raw.get("command") or raw.get("cmd")
        self.stop_command = raw.get("stop_command") or raw.get("stop_cmd")
        self.env = {str(k): _str_env(v) for k, v in (raw.get("env") or {}).items()}
        self.depends_on = _as_list(raw.get("depends_on"))
        self.port = raw.get("port")
        if self.port is None and raw.get("ports"):
            first = _as_list(raw.get("ports"))[0]
            try:
                self.port = int(str(first).split(":")[0])
            except (ValueError, IndexError):
                self.port = None
        if isinstance(self.port, str) and self.port.isdigit():
            self.port = int(self.port)
        if self.port is not None and not isinstance(self.port, int):
            self.port = DEFAULT_PORTS.get(str(self.port), self.port)

        self.ready, self.ready_timeout = self._parse_ready(raw.get("ready"))
        self.stop_signal = _signal_of(raw.get("stop_signal", "SIGTERM"))
        self.stop_timeout = float(raw.get("stop_timeout", 10))
        self.build = _as_list(raw.get("build"))
        self.install = _as_list(raw.get("install"))
        self.clean = _as_list(raw.get("clean"))
        self.clean_globs = _as_list(raw.get("clean_globs"))
        self.docker_services = _as_list(raw.get("services") or raw.get("containers"))
        self.docker_files = _as_list(raw.get("files") or raw.get("compose"))
        self.docker_project = raw.get("project_name") or raw.get("compose_project")
        self.optional = bool(raw.get("optional", False))
        self.description = raw.get("description", "")
        self.flags = _as_list(raw.get("flags"))

        # Per-service window policy: `terminal: false` never opens a window (a
        # database container does not need one), `terminal: {hold: false}` overrides
        # a single key of the global terminal config.
        raw_term = raw.get("terminal")
        if raw_term is False:
            self.terminal_enabled = False
            self.terminal_overrides: dict = {}
        elif isinstance(raw_term, dict):
            self.terminal_enabled = bool(raw_term.get("enabled", True))
            self.terminal_overrides = raw_term
        elif raw_term is True or raw_term is None:
            self.terminal_enabled = True
            self.terminal_overrides = {}
        else:
            raise ConfigError(f"service {self.name!r}: `terminal` must be true/false or a mapping")
        self.terminal_title = self.terminal_overrides.get("title")
        self.terminal_driver = self.terminal_overrides.get("driver")

    @property
    def terminal_hold(self) -> bool:
        if "hold" in self.terminal_overrides:
            return bool(self.terminal_overrides["hold"])
        return self.project.terminal["hold"]

    def wants_window(self, requested: bool) -> bool:
        """Should this service get a terminal window right now?

        A window only makes sense for a long-running local process: docker groups
        and one-shot commands have nothing to watch in a window.
        """
        return requested and self.kind == "proc" and self.terminal_enabled

    def _parse_ready(self, ready):
        if ready is None:
            return ("port" if self.kind == "proc" and self.port else None), 60.0
        if isinstance(ready, str):
            if ready in ("port", "tcp", "http"):
                return (ready, 60.0)
            if ready.startswith("http://") or ready.startswith("https://"):
                return (("http", ready), 90.0)
            return (("cmd", ready), 60.0)
        if isinstance(ready, dict):
            timeout = float(ready.get("timeout", 60))
            if "http" in ready:
                return (("http", str(ready["http"])), timeout)
            if "tcp" in ready:
                return (("tcp", int(ready["tcp"])), timeout)
            if "port" in ready:
                return (("port", int(ready["port"])), timeout)
            if "cmd" in ready:
                return (("cmd", str(ready["cmd"])), timeout)
            # `ready: {timeout: 600}` means "nothing to poll" — legitimate for a
            # service that needs a long build but exposes no endpoint.
            return (None, timeout)
        if isinstance(ready, int):
            return (("port", ready), 60.0)
        raise ConfigError(f"service {self.name!r}: unsupported `ready` value {ready!r}")

    @property
    def effective_port(self) -> int | None:
        if isinstance(self.port, int):
            return self.port
        if self.ready and self.ready[0] in ("port", "tcp"):
            return int(self.ready[1])
        return None

    def resolved_command(self) -> str | None:
        if self.kind != "proc" or not self.command:
            return None
        return str(self.command).strip()

    def log_path(self) -> Path:
        return self.project.logs_dir / f"{self.name}.log"

    def state_path(self) -> Path:
        return self.project.run_dir / f"{self.name}.json"


def _str_env(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [v]


def _signal_of(name) -> int:
    if isinstance(name, int):
        return name
    text = str(name).upper()
    if not text.startswith("SIG"):
        text = "SIG" + text
    sig = getattr(signal, text, None)
    if sig is None:
        raise ConfigError(f"unknown signal {name!r}")
    return int(sig)


class Project:
    def __init__(self, config_path: Path, data: dict):
        self.config_path = config_path
        self.root = config_path.parent
        dotted = config_path.parent.name
        if dotted in (".devpad", ".devpad"):
            self.root = config_path.parent.parent
        self.data = data or {}

        # Keep an existing devpad setup byte-for-byte compatible: its runtime data
        # stays in .devpad, so nothing moves under a user who upgrades.
        legacy = config_path.name.startswith("devpad") or dotted == ".devpad"
        self.runtime_dir = self.root / (".devpad" if legacy else ".devpad")
        self.run_dir = self.runtime_dir / "run"
        self.logs_dir = self.runtime_dir / "logs"
        self.terminal_dir = self.runtime_dir / "terminal"
        self.pids_dir = self.runtime_dir / "pids"

        defaults = self.data.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ConfigError("`defaults` must be a mapping")
        self.defaults = defaults

        raw_terminal = self.data.get("terminal") or {}
        if not isinstance(raw_terminal, dict):
            raise ConfigError("`terminal` must be a mapping")
        self.terminal = {
            "driver": str(raw_terminal.get("driver", "auto")),
            "hold": bool(raw_terminal.get("hold", True)),
            "title": str(raw_terminal.get("title", "{project} · {service}")),
            "reuse": bool(raw_terminal.get("reuse", False)),
        }
        # The project's display name for window titles.
        self.name = str(self.data.get("name") or self.root.name)

        # A `draft: true` config is one `detect.py` generated but a human has not
        # finished. Only that explicit marker relaxes the non-empty check, so a
        # hand-written config with a services typo still fails loudly.
        self.draft = bool(self.data.get("draft"))
        raw_services = self.data.get("services") or {}
        if not isinstance(raw_services, dict) or (not raw_services and not self.draft):
            raise ConfigError(
                "config needs a non-empty `services` mapping"
                if not raw_services
                else "`services` must be a mapping of name -> definition"
            )
        self.services: dict[str, Service] = {}
        for name, raw in raw_services.items():
            merged = dict(defaults)
            if isinstance(raw, dict):
                merged.update(raw)
            svc = Service(str(name), merged, self)
            self.services[str(name)] = svc
        for svc in self.services.values():
            for dep in svc.depends_on:
                if dep not in self.services:
                    raise ConfigError(f"service {svc.name!r} depends on unknown service {dep!r}")
            if svc.kind in ("proc", "cmd") and not svc.command:
                raise ConfigError(f"service {svc.name!r}: kind {svc.kind} needs `command`")

    def window_title(self, svc: "Service") -> str:
        template = svc.terminal_title or self.terminal["title"]
        return (
            template.replace("{project}", self.name)
            .replace("{service}", svc.name)
            .replace("{kind}", svc.kind)
        )

    def ensure_dirs(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.terminal_dir.mkdir(parents=True, exist_ok=True)
        self.pids_dir.mkdir(parents=True, exist_ok=True)

    def order(self, names: list[str] | None = None) -> list[Service]:
        """Topological order over depends_on, stable by config order."""
        selected = set(names) if names else set(self.services)
        missing = selected - set(self.services)
        if missing:
            raise ConfigError(f"unknown service(s): {', '.join(sorted(missing))}")
        if names:
            for n in list(selected):
                selected |= self._closure(n) - {n}
        result: list[str] = []
        temp: set[str] = set()
        perm: set[str] = set()

        def visit(n: str, trail: list[str]):
            if n in perm or n not in selected:
                return
            if n in temp:
                raise ConfigError("dependency cycle: " + " -> ".join(trail + [n]))
            temp.add(n)
            for dep in self.services[n].depends_on:
                visit(dep, trail + [n])
            temp.discard(n)
            perm.add(n)
            result.append(n)

        for n in self.services:
            visit(n, [])
        return [self.services[n] for n in result]

    def _closure(self, name: str) -> set[str]:
        seen: set[str] = set()
        stack = [name]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.services[cur].depends_on)
        return seen


# ---------------------------------------------------------------------------
# runtime state
# ---------------------------------------------------------------------------


def read_state(svc: Service) -> dict | None:
    p = svc.state_path()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_state(svc: Service, state: dict):
    svc.project.ensure_dirs()
    tmp = svc.state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(svc.state_path())


def clear_state(svc: Service):
    try:
        svc.state_path().unlink()
    except FileNotFoundError:
        pass


_LIBPROC = None
_LIBPROC_TRIED = False


def _libproc():
    """macOS libproc, loaded once. None on other platforms or when unavailable."""
    global _LIBPROC, _LIBPROC_TRIED
    if _LIBPROC_TRIED:
        return _LIBPROC
    _LIBPROC_TRIED = True
    if sys.platform != "darwin":
        return None
    try:
        import ctypes.util

        path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        _LIBPROC = ctypes.CDLL(path, use_errno=True)
    except (OSError, ImportError):
        _LIBPROC = None
    return _LIBPROC


def _proc_state(pid: int) -> str | None:
    """Single-letter process state from the kernel: 'R','S','D','Z','T',...

    A zombie ('Z') is the case that matters. `os.kill(pid, 0)` succeeds on a
    zombie, so without this check a service that crashed instantly looks alive and
    a readiness probe waits out its entire timeout instead of reporting the failure.
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as fh:
            rest = fh.read().rsplit(")", 1)[-1].split()
        return rest[0] if rest else None
    except (OSError, IndexError):
        return None
    # macOS has no /proc; libproc's pidinfo is used below instead.


def _proc_bsdinfo(pid: int):
    """(state, start_sec, start_usec, ppid) from libproc on macOS, else None."""
    proc = _libproc()
    if proc is None:
        return None
    try:
        buf = ctypes.create_string_buffer(160)
        n = proc.proc_pidinfo(int(pid), 1, 0, buf, 160)  # PROC_PIDTBSDINFO
        if n <= 0:
            return None
        import struct

        # struct proc_bsdinfo: pbi_flags, pbi_status, pbi_xstatus, pbi_pid, pbi_ppid,
        # pbi_uid, pbi_gid, pbi_ruid, pbi_rgid, pbi_svuid, pbi_svgid, rfu_1,
        # pbi_comm[16], pbi_name[32], then pbi_start_tvsec / pbi_start_tvusec.
        ppid = struct.unpack_from("<I", buf.raw, 16)[0]
        status = struct.unpack_from("<I", buf.raw, 4)[0]
        sec = struct.unpack_from("<Q", buf.raw, 120)[0]
        usec = struct.unpack_from("<Q", buf.raw, 128)[0]
        return status, sec, usec, ppid
    except Exception:
        return None


def _proc_created_at(pid: int) -> float | None:
    """Process start time in epoch seconds, or None when it cannot be read.

    Called on the start path, so it swallows every error: being unable to date a
    process must never be the reason a service fails to start.
    """
    try:
        bsd = _proc_bsdinfo(pid)
        if bsd is not None:
            _, sec, usec, _ = bsd
            return float(sec) + float(usec) / 1e6 if sec else None
        with open(f"/proc/{pid}/stat", "r") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
        starttime = int(fields[19])
        with open("/proc/stat", "r") as fh:
            for line in fh:
                if line.startswith("btime"):
                    return int(line.split()[1]) + starttime / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None
    return None


def pid_alive(pid: int) -> bool:
    """True only for a live, runnable process. A zombie counts as dead."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else: alive for our purposes
    state = _proc_state(pid)
    if state is not None:
        return state != "Z"
    bsd = _proc_bsdinfo(pid)
    if bsd is not None:
        status = bsd[0]
        return status != 5  # SZOMB
    return True


def reap_child(svc: "Service") -> int | None:
    """Collect our exited child and record its exit code.

    Only the parent can reap or observe a child's status, and devpad is the parent
    (it spawned the service via Popen). Without this the exit code is never known
    and `wait_ready` cannot report a crash. waitpid raises ChildProcessError for a
    pid that is not ours, so this is safe to call on any recorded state.
    """
    state = read_state(svc) or {}
    if state.get("exit_code") is not None:
        return state["exit_code"]
    pid = state.get("pid")
    if not pid:
        return None
    try:
        waited, status = os.waitpid(int(pid), os.WNOHANG)
    except (ChildProcessError, OSError):
        # Not our child (e.g. state from an earlier session): fall back to liveness.
        return state.get("exit_code")
    if waited == 0:
        return None
    code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
    state["exit_code"] = code
    state["exited_at"] = time.time()
    write_state(svc, state)
    return code


def pid_pgid(pid: int) -> int | None:
    """Process-group id of pid, via the kernel. None when unreadable."""
    try:
        return os.getpgid(int(pid))
    except (ProcessLookupError, PermissionError, OSError):
        return None


def pid_command(pid: int) -> str:
    """Best-effort executable identity for a pid.

    Reads the kernel directly (libproc on macOS, /proc on Linux); `ps` is only a
    fallback because some sandboxes refuse to spawn it. Returns "" when the
    process has no accessible identity — never raises, never guesses.
    """
    if pid <= 0:
        return ""
    proc = _libproc()
    if proc is not None:
        try:
            buf = ctypes.create_string_buffer(4096)
            n = proc.proc_pidpath(int(pid), buf, 4096)
            if n > 0:
                return buf.value.decode("utf-8", "replace")
        except Exception:
            pass
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    out = run_capture(["ps", "-o", "command=", "-p", str(pid)]).strip()
    return "" if out.startswith("<") else out


def _identity_variants(token: str) -> set[str]:
    """Names that should count as the same program as `token`.

    `python3` must accept a binary named `python3.14` or `python3.12.exe`; it must
    NOT accept `python`. Versioned interpreter symlinks are the common case here.
    """
    base = os.path.basename(token).lower()
    if not base:
        return set()
    out = {base}
    if base.endswith(".exe"):
        out.add(base[:-4])
    return out


def _looks_like(actual: str, expected_token: str) -> bool:
    """True when an executable path or argv token is the expected program."""
    a = os.path.basename(actual).lower()
    for want in _identity_variants(expected_token):
        if a == want:
            return True
        # versioned binaries: python3 -> python3.14, node -> node20
        for sep in (".", "-", "_"):
            if a.startswith(want + sep) and a[len(want) + 1 : len(want) + 2].isdigit():
                return True
        if a.startswith(want) and a[len(want) :].isdigit():
            return True
    return False


def pid_matches(pid: int, command: str | None) -> bool:
    """True when pid plausibly is the process we would launch today.

    Only positive evidence authorizes touching a process; an unidentifiable pid
    reports False so the port guard, not a guess, makes the decision.
    """
    if not command:
        return False
    try:
        expected = shlex.split(command)[0]
    except (ValueError, IndexError):
        return False
    actual = pid_command(pid)
    if not actual:
        return False
    if _looks_like(actual, expected):
        return True
    for tok in actual.split():
        if _looks_like(tok, expected):
            return True
    return False


# Launchers that hide the real program from `pid_command`. devpad always spawns
# through `/bin/sh -lc`, so identity matching alone can never recognise the pid of
# one of these; the process-group and start-time proofs are what cover them.
WRAPPER_COMMANDS = {"sh", "bash", "zsh", "env", "npx", "npx.cmd", "pnpm", "npm", "yarn", "corepack", "pnpx", "make"}


def command_is_wrapper(command: str | None) -> bool:
    if not command:
        return False
    try:
        first = os.path.basename(shlex.split(command)[0]).lower()
    except (ValueError, IndexError):
        return False
    return first in WRAPPER_COMMANDS or first.endswith((".sh", ".cmd", ".bat"))


def proc_owned(svc: Service, state: dict) -> bool:
    """Do we still own the process recorded in `state`?

    Three independent proofs, any one sufficient:
      1. process-group id is the one we created — start_new_session made pid==pgid;
      2. the executable identity still matches today's command;
      3. the process start time matches what we recorded at spawn.

    A wrapper such as `npx react-native start` fails (2) because it hides behind
    `sh`, which is exactly why (1) and (3) exist. A false negative here would leave
    a dev server running, so the check must not be clever — just well-evidenced.
    """
    pid = state.get("pid")
    if not pid:
        return False
    pid = int(pid)
    if not pid_alive(pid):
        return False
    recorded_pgid = state.get("pgid")
    actual_pgid = pid_pgid(pid)
    if recorded_pgid and actual_pgid and int(recorded_pgid) == int(actual_pgid):
        return True
    created = state.get("start_created")
    if created:
        actual = _proc_created_at(pid)
        if actual is not None and abs(actual - float(created)) <= 2.0:
            return True
    return pid_matches(pid, svc.resolved_command())


def run_capture(cmd: list[str], cwd: Path | None = None, timeout: float = 30) -> str:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<{type(exc).__name__}: {exc}>"


def port_pids(port: int) -> list[int]:
    out = run_capture(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    pids = []
    for tok in out.split():
        if tok.isdigit() and int(tok) not in pids:
            pids.append(int(tok))
    return pids


def port_free(port: int) -> bool:
    return not port_pids(port)


# ---------------------------------------------------------------------------
# readiness probes
# ---------------------------------------------------------------------------


# Both loopback families must be probed. Vite 7 binds `[::1]` only, so a probe
# against 127.0.0.1 reports "not ready" for a server that has been up for 200ms —
# and devpad would then wait out the whole timeout and declare a healthy service
# failed. `localhost` is deliberately not used: on macOS it resolves to whichever
# family the resolver prefers, which is exactly the ambiguity being avoided.
LOOPBACKS = ("127.0.0.1", "::1")


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(scheme: str, host: str, port: int | None, path: str, timeout: float = 1.0) -> bool:
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    try:
        conn = conn_cls(host, port, timeout=timeout)
    except (OSError, ValueError):
        return False
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read(64)
        return resp.status < 500
    except (OSError, http.client.HTTPException):
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def probe_once(kind: str, target) -> bool:
    try:
        if kind in ("port", "tcp"):
            port = int(target)
            return any(_tcp_open(host, port) for host in LOOPBACKS)
        if kind == "http":
            url = str(target)
            parsed = urlparse(url)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            scheme = parsed.scheme or "http"
            hosts = [parsed.hostname or "127.0.0.1"]
            # A URL written as 127.0.0.1 must still match an IPv6-only server, and
            # vice versa; the config names a service, not a network family.
            if hosts[0] in LOOPBACKS or hosts[0] == "localhost":
                hosts += [h for h in LOOPBACKS if h != hosts[0]]
                hosts += ["localhost"]
            return any(_http_ok(scheme, host, parsed.port, path) for host in hosts)
        if kind == "cmd":
            proc = subprocess.run(
                str(target), shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
            )
            return proc.returncode == 0
    except Exception:
        return False
    return False


def wait_ready(svc: Service, timeout: float | None = None, log=print) -> bool:
    kind, target = svc.ready if svc.ready else (None, None)

    def died() -> str | None:
        """Did the service die? Works for both managed and windowed services.

        A windowed service is not our child, so waitpid cannot report it; its pid
        file is the only evidence, and an exit code is unavailable there.
        """
        state = read_state(svc) or {}
        if state.get("mode") == "terminal":
            pid = state.get("pid")
            if pid and not pid_alive(int(pid)):
                return "process exited (window still open with its output)"
            return None
        code = reap_child(svc)
        return None if code is None else f"exited with code {code}"

    def report(reason: str):
        log(f"    !! {reason}")
        log(f"       last output: devpad logs {svc.name} -n 40 --no-prefix")

    if not kind:
        # Still catch an instant crash, so `up` does not report success for a
        # service that died while nobody was looking.
        time.sleep(0.25)
        reason = died()
        if reason:
            report(reason)
            return False
        return True

    limit = svc.ready_timeout if timeout is None else timeout
    deadline = time.monotonic() + limit
    announced = False
    while time.monotonic() < deadline:
        if probe_once(kind, target):
            log(f"    ready ({kind}: {target})")
            return True
        reason = died()
        if reason:
            report(reason + " before becoming ready")
            return False
        if not announced:
            log(f"    waiting for {kind}: {target} (timeout {limit:g}s)")
            announced = True
        time.sleep(0.4)
    log(f"    !! not ready after {limit:g}s ({kind}: {target})")
    return False


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


def compose_cmd(svc: Service) -> list[str]:
    base = ["docker", "compose"]
    for f in svc.docker_files:
        base += ["-f", str(f)]
    if svc.docker_project:
        base += ["-p", str(svc.docker_project)]
    return base


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_daemon_up() -> bool:
    if not docker_available():
        return False
    proc = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def start_proc(svc: Service, log=print) -> bool:
    state = read_state(svc) or {}
    pid = state.get("pid")
    if pid and proc_owned(svc, state):
        log(f"  = {svc.name}: already running (pid {pid})")
        return True
    if pid and pid_alive(int(pid)):
        # The recorded pid was recycled by an unrelated process: fall through and
        # let the port guard give the real diagnosis instead of a lie.
        log(f"  ~ {svc.name}: recorded pid {pid} is not `{svc.resolved_command()}` anymore; forgetting it")
        clear_state(svc)
    elif state:
        clear_state(svc)

    port = svc.effective_port
    if port and not port_free(port):
        holders = port_pids(port)
        detail = ", ".join(f"{p} ({pid_command(p) or 'unknown'})" for p in holders) or "unknown"
        log(f"  ! {svc.name}: port {port} already in use by: {detail}")
        log(f"    inspect: devpad ports {port}   |   free it: devpad kill {port}")
        return False

    svc.project.ensure_dirs()
    logfile = svc.log_path()
    # Sample the wall clock immediately before spawning, so the recorded start time
    # can later be matched against the kernel's own view of when the pid started.
    spawn_wall = time.time()
    with open(logfile, "ab") as fh:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== devpad start {stamp} =====\n".encode())
        fh.flush()
        env = os.environ.copy()
        env.update(svc.env)
        env.setdefault("FORCE_COLOR", "1")
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-lc", svc.resolved_command() or ""],
                cwd=str(svc.dir),
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,  # own process group => group kill works
                close_fds=True,
            )
        except OSError as exc:
            log(f"  !! {svc.name}: failed to spawn: {exc}")
            return False

    spawn_created = _proc_created_at(proc.pid) or spawn_wall
    write_state(
        svc,
        {
            "name": svc.name,
            "kind": svc.kind,
            "pid": proc.pid,
            # start_new_session makes the child a session+group leader, so the pgid
            # equals its pid; read it back rather than trusting the convention.
            "pgid": pid_pgid(proc.pid) or proc.pid,
            "port": port,
            "dir": str(svc.dir),
            "command": svc.resolved_command(),
            "wrapper": command_is_wrapper(svc.resolved_command()),
            "started_at": spawn_wall,
            "start_created": spawn_created,
            "log": str(logfile),
            "exit_code": None,
        },
    )
    log(f"  > {svc.name}: started (pid {proc.pid})  log: {rel(svc.project, logfile)}")
    return True


def start_docker(svc: Service, log=print, build: bool | None = None) -> bool:
    if not docker_daemon_up():
        log(f"  !! {svc.name}: docker daemon is not reachable")
        return False
    cmd = compose_cmd(svc) + ["up", "-d"]
    if build is None:
        build = "build" in svc.flags
    if build:
        cmd.append("--build")
    for flag in svc.flags:
        if flag in ("build", "force-recreate"):
            cmd.append(f"--{flag}")
    cmd += [str(s) for s in svc.docker_services]
    log(f"  > {svc.name}: {' '.join(cmd)}")
    rc = stream(cmd, cwd=svc.dir, prefix=f"{svc.name}: ", log=log)
    if rc != 0:
        log(f"  !! {svc.name}: docker compose up failed (exit {rc})")
        return False
    write_state(
        svc,
        {
            "name": svc.name,
            "kind": "docker",
            "pid": None,
            "services": svc.docker_services,
            "started_at": time.time(),
            "log": None,
            "exit_code": 0,
        },
    )
    log(f"  > {svc.name}: containers up")
    return True


def run_cmd_service(svc: Service, log=print, timeout: float | None = None) -> bool:
    log(f"  > {svc.name}: {svc.command}")
    rc = stream(["/bin/sh", "-lc", str(svc.command)], cwd=svc.dir, prefix=f"{svc.name}: ", log=log, env=svc.env)
    if rc != 0:
        log(f"  !! {svc.name}: failed (exit {rc})")
        return False
    log(f"  > {svc.name}: done")
    return True


def _terminals():
    """Import scripts/terminals.py next to this file, whatever the cwd."""
    global _TERMINALS_MOD
    if _TERMINALS_MOD is None:
        import importlib.util

        path = Path(__file__).resolve().parent / "terminals.py"
        if not path.is_file():
            raise ConfigError(f"terminals.py not found next to devpad.py ({path})")
        spec = importlib.util.spec_from_file_location("devpad_terminals", path)
        if spec is None or spec.loader is None:
            raise ConfigError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TERMINALS_MOD = module
    return _TERMINALS_MOD


_TERMINALS_MOD = None


def _wait_for_pid(pid_file: Path, timeout: float) -> int | None:
    """The runner writes its own pid; the window is spawned by another app, so this
    file is the only way to learn which process to track and later stop."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = pid_file.read_text().strip()
            if text.isdigit():
                pid = int(text)
                if pid_alive(pid):
                    return pid
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return None


def start_in_terminal(svc: Service, log=print) -> bool:
    """Open a visible terminal window and run the service inside it.

    This is the whole point of devpad: the log must land somewhere the user can
    read, scroll and copy — their own terminal — rather than in the buffer of
    whatever process happened to start it.
    """
    term = _terminals()
    requested = svc.terminal_driver or svc.project.terminal["driver"]
    driver, why = term.resolve_driver(requested)
    if driver is None:
        log(f"  !! {svc.name}: no usable terminal driver — {why}")
        log(f"     run `devpad doctor` for the full driver matrix")
        return False

    title = svc.project.window_title(svc)
    pid_file = svc.project.pids_dir / f"{svc.name}.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass

    logfile = svc.log_path()
    svc.project.ensure_dirs()
    with open(logfile, "ab") as fh:
        fh.write(f"\n===== devpad start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())

    spec = term.RunnerSpec(
        title=title,
        cwd=svc.dir,
        command=svc.resolved_command() or "",
        hold=svc.terminal_hold,
        env=svc.env,
        pid_file=pid_file,
        # Record the window's output too: the human reads the window, an agent reads
        # this file. Without it, windowed mode is opaque to `devpad logs`.
        log_path=logfile,
    )
    runner = term.write_runner(spec, svc.project.terminal_dir)
    log(f"  > {svc.name}: opening window ({driver.name}) — {title}")
    handle = driver.spawn(spec, runner)
    if handle is None:
        log(f"  !! {svc.name}: the window did not start")
        return False

    pid = _wait_for_pid(pid_file, timeout=12)
    pgid = pid_pgid(pid) if pid else None
    write_state(
        svc,
        {
            "name": svc.name,
            "kind": svc.kind,
            "mode": "terminal",
            "pid": pid,
            "pgid": pgid or pid,
            "port": svc.effective_port,
            "dir": str(svc.dir),
            "command": svc.resolved_command(),
            "started_at": time.time(),
            "window": handle,
            "driver": driver.name,
            "title": title,
            "log": str(logfile),
            "exit_code": None,
        },
    )
    if pid:
        log(f"    window ready (pid {pid}, {driver.name})")
    else:
        # The window opened but never reported a pid: the command may have died
        # instantly. Say so instead of pretending it started cleanly.
        log(f"    !! window opened but no pid reported; the command may have exited immediately")
    return True


def close_window(svc: Service, state: dict, log=print) -> bool:
    """Close the window a service was given. Best-effort and honest about it."""
    handle = state.get("window") or {}
    if not handle:
        return True
    term = _terminals()
    driver = term.get_driver(handle.get("driver") or "")
    if driver is None:
        return False
    if not driver.supports_close:
        log(f"  ~ {svc.name}: {driver.name} windows cannot be closed by devpad; close it yourself")
        return False
    if driver.close(handle):
        log(f"  < {svc.name}: window closed")
        return True
    log(f"  ~ {svc.name}: could not close the window (it may already be gone)")
    return False


def stop_in_terminal(svc: Service, state: dict, log=print) -> bool:
    """Stop a windowed service: signal the process group first, then close the window.

    That order matters. Closing the window first would leave the process running
    with no way to see it, and the port would stay bound — the classic orphan that
    makes the next start fail with "address already in use".
    """
    pid = state.get("pid")
    ok = True
    if pid and pid_alive(int(pid)):
        kill_group(int(pid), svc.stop_signal)
        deadline = time.monotonic() + svc.stop_timeout
        while time.monotonic() < deadline and pid_alive(int(pid)):
            time.sleep(0.15)
        if pid_alive(int(pid)):
            log(f"  ~ {svc.name}: still alive after {svc.stop_timeout:g}s, sending SIGKILL")
            kill_group(int(pid), signal.SIGKILL)
            time.sleep(0.3)
    elif pid:
        log(f"  = {svc.name}: process already exited")

    if svc.port:
        leftovers = port_pids(int(svc.port))
        if leftovers:
            log(f"  ~ {svc.name}: freeing port {svc.port} (pid {leftovers})")
            for p in leftovers:
                kill_group(p, signal.SIGKILL)

    close_window(svc, state, log=log)
    clear_state(svc)
    log(f"  < {svc.name}: stopped")
    return ok


def stop_proc(svc: Service, log=print) -> bool:
    state = read_state(svc) or {}
    if state.get("mode") == "terminal":
        if not state.get("pid") and not state.get("window"):
            clear_state(svc)
            log(f"  = {svc.name}: not running")
            return True
        return stop_in_terminal(svc, state, log=log)
    pid = state.get("pid")
    if pid and not proc_owned(svc, state):
        holders = port_pids(int(svc.port)) if svc.port else []
        if int(pid) not in holders:
            log(f"  = {svc.name}: recorded pid {pid} is not this service and holds no port; forgetting it")
            pid = None
    if not pid:
        if svc.port and not port_free(int(svc.port)):
            holders = port_pids(int(svc.port))
            log(f"  ! {svc.name}: no recorded process, but port {svc.port} is held by {holders}")
            log(f"    use: devpad kill {svc.port}")
            return False
        log(f"  = {svc.name}: not running")
        clear_state(svc)
        return True
    pid = int(pid)

    if svc.stop_command:
        stream(["/bin/sh", "-lc", str(svc.stop_command)], cwd=svc.dir, prefix=f"{svc.name}: ", log=log)

    if not pid_alive(pid):
        log(f"  = {svc.name}: already exited")
        clear_state(svc)
        return True

    kill_group(pid, svc.stop_signal)
    deadline = time.monotonic() + svc.stop_timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.15)
    if pid_alive(pid):
        log(f"  ~ {svc.name}: still alive after {svc.stop_timeout:g}s, sending SIGKILL")
        kill_group(pid, signal.SIGKILL)
        time.sleep(0.3)
    # Anything still bound to our declared port gets cleared too.
    if svc.port:
        leftovers = port_pids(int(svc.port))
        if leftovers:
            log(f"  ~ {svc.name}: freeing port {svc.port} (pid {leftovers})")
            for p in leftovers:
                kill_group(p, signal.SIGTERM)
            time.sleep(0.4)
            for p in port_pids(int(svc.port)):
                kill_group(p, signal.SIGKILL)
    clear_state(svc)
    log(f"  < {svc.name}: stopped")
    return True


def stop_docker(svc: Service, log=print, down: bool = True) -> bool:
    if not docker_available():
        log(f"  = {svc.name}: docker not installed, nothing to stop")
        clear_state(svc)
        return True
    cmd = compose_cmd(svc) + (["down"] if down else ["stop"])
    cmd += [str(s) for s in svc.docker_services]
    log(f"  < {svc.name}: {' '.join(cmd)}")
    stream(cmd, cwd=svc.dir, prefix=f"{svc.name}: ", log=log)
    clear_state(svc)
    return True


def kill_group(pid: int, sig: int):
    """Signal the whole process group when possible, else just the process."""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except PermissionError:
        pgid = None
    if pgid and pgid != os.getpgid(0):
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def stream(cmd: list[str], cwd: Path | None = None, prefix: str = "", log=print, env: dict | None = None) -> int:
    """Run a command to completion, relaying output live with a prefix."""
    full_env = os.environ.copy()
    if env:
        full_env.update({k: _str_env(v) for k, v in env.items()})
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=full_env,
        )
    except OSError as exc:
        log(f"{prefix}<spawn failed: {exc}>")
        return 127
    assert proc.stdout is not None
    for raw in proc.stdout:
        log(prefix + raw.decode("utf-8", "replace").rstrip("\n"))
    return proc.wait()


def rel(project: Project, path: Path) -> str:
    try:
        return str(path.relative_to(project.root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# high level operations
# ---------------------------------------------------------------------------


def svc_running(svc: Service) -> bool:
    state = read_state(svc)
    if not state:
        return False
    if state.get("kind") == "docker":
        return docker_container_running(svc)
    return proc_owned(svc, state)


def docker_container_running(svc: Service) -> bool:
    if not docker_available():
        return False
    ids = compose_cmd(svc) + ["ps", "-q", *[str(s) for s in svc.docker_services]]
    out = run_capture(ids, cwd=svc.dir).strip()
    if not out:
        return False
    running = run_capture(["docker", "inspect", "-f", "{{.State.Running}}", *out.split()])
    return "true" in running.split()


def do_start(services: list[Service], log=print, force: bool = False, terminal: bool = False) -> bool:
    """Start services in order, waiting for each to become ready.

    A service counts as started only when it is *also* ready. Returning success for
    a process that spawned and then died would make `devpad up` report a healthy
    stack while the service is down, which is the one lie this tool must never tell.

    With `terminal=True`, proc services are started in their own visible window
    instead of detached with output redirected to a file.
    """
    ok = True
    for svc in services:
        if svc.kind == "cmd":
            if run_cmd_service(svc, log=log):
                continue
            if svc.optional:
                log(f"  = {svc.name}: optional, continuing")
                continue
            return False

        if force and svc.kind == "proc":
            stop_proc(svc, log=lambda *_: None)

        if svc.kind == "docker":
            started = start_docker(svc, log=log, build=True if force else None)
        elif svc.wants_window(terminal):
            if already_windowed(svc, log=log):
                started = True
            else:
                started = start_in_terminal(svc, log=log)
        else:
            started = start_proc(svc, log=log)
        ready = wait_ready(svc, log=log) if started else False

        if not (started and ready):
            if svc.optional:
                log(f"  = {svc.name}: optional, continuing")
                continue
            ok = False
            break
    return ok


def already_windowed(svc: Service, log=print) -> bool:
    """A windowed service that is still alive must not get a second window."""
    state = read_state(svc) or {}
    if state.get("mode") != "terminal":
        return False
    pid = state.get("pid")
    if pid and pid_alive(int(pid)):
        log(f"  = {svc.name}: already running in its window (pid {pid})")
        return True
    return False


def do_stop(services: list[Service], log=print, down: bool = True) -> bool:
    ok = True
    for svc in reversed(services):
        if svc.kind == "docker":
            ok = stop_docker(svc, log=log, down=down) and ok
        elif svc.kind == "cmd":
            continue
        else:
            ok = stop_proc(svc, log=log) and ok
    return ok


def do_restart(services: list[Service], log=print, force: bool = False, terminal: bool = False) -> bool:
    stopped = do_stop(services, log=log, down=False)
    log("")
    started = do_start(services, log=log, force=force, terminal=terminal)
    return stopped and started


def expand_targets(project: Project, names: list[str]) -> list[Service]:
    if not names:
        return project.order()
    return project.order(names)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_up(project: Project, args) -> int:
    project.ensure_dirs()
    names = args.services or []
    try:
        services = project.order(names or None)
    except ConfigError as exc:
        print(f"devpad: {exc}")
        return 2
    ok = do_start(services, force=args.force, terminal=getattr(args, "terminal", False))
    print("")
    cmd_status(project, args, quiet=False)
    if args.attach:
        return cmd_logs(project, argparse.Namespace(services=[], follow=True, lines=0, prefix=True))
    return 0 if bool(ok) else 1


def cmd_down(project: Project, args) -> int:
    services = list(reversed(project.order(None)))
    if args.services:
        services = project.order(args.services)
    ok = do_stop(services, down=not args.keep_containers)
    print("")
    cmd_status(project, args, quiet=False)
    return 0 if bool(ok) else 1


def cmd_restart(project: Project, args) -> int:
    """Stop and start the named services, leaving their dependencies running.

    `restart api` must restart the API and nothing else. Expanding the set to its
    `depends_on` closure before stopping would also bounce the database, dropping
    every connection the rest of the stack holds — a far larger action than the one
    requested. Dependencies are therefore pulled in for the *start* half only, where
    they are already running and starting them again is a no-op.
    """
    names = args.services or []
    terminal = getattr(args, "terminal", False)
    if names:
        missing = [n for n in names if n not in project.services]
        if missing:
            print(f"devpad: unknown service(s): {', '.join(sorted(missing))}")
            return 2
        stop_targets = [project.services[n] for n in names]
        start_targets = project.order(names)
        stopped = do_stop(list(reversed(stop_targets)), down=False)
        print("")
        ok = do_start(start_targets, force=args.force, terminal=terminal) and stopped
    else:
        ok = do_restart(project.order(None), force=args.force, terminal=terminal)
    print("")
    cmd_status(project, args, quiet=False)
    return 0 if bool(ok) else 1


def cmd_status(project: Project, args, quiet: bool = False) -> int:
    rows = []
    for svc in project.order(None):
        state = read_state(svc) or {}
        running = svc_running(svc)
        pid = state.get("pid")
        uptime = ""
        if running and state.get("started_at"):
            up = time.time() - float(state["started_at"])
            uptime = f"{int(up // 3600)}h{int(up % 3600 // 60)}m" if up >= 3600 else f"{int(up // 60)}m{int(up % 60)}s"
        port = svc.effective_port
        port_state = ""
        if port:
            holders = port_pids(int(port))
            port_state = f"{port}" + (f" ({holders[0]})" if holders else " free")
        health = ""
        if running and svc.ready and svc.ready[0]:
            health = "ready" if probe_once(*svc.ready) else "starting?"
        window = "-"
        if state.get("mode") == "terminal":
            handle = state.get("window") or {}
            window = state.get("driver") or handle.get("driver") or "window"
            if handle.get("tty"):
                window += f" {handle['tty'].rsplit('/', 1)[-1]}"
            elif handle.get("session"):
                window += f" {handle['session']}"
        rows.append(
            [
                svc.name,
                "up" if running else "down",
                str(pid) if pid else "-",
                uptime or "-",
                port_state or "-",
                health or "-",
                window,
                f"{svc.kind}/{svc.description or ''}".rstrip("/"),
            ]
        )
    headers = ["SERVICE", "STATE", "PID", "UP", "PORT", "HEALTH", "WINDOW", "KIND"]
    table = _table(headers, rows)
    if not quiet:
        print(f"config: {rel(project, project.config_path)}")
        print(table)
    return 0


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(no services)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    for row in rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(out)




_LOG_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")


def _read_log_lines(path: Path) -> list[str]:
    """Read a log as clean lines.

    Detached logs are plain text, but a windowed service's log is a `script(1)`
    pty transcript: CRLF endings, ANSI colour codes, and stray backspaces. Showing
    that raw would make `devpad logs` unreadable, which defeats its purpose.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = _LOG_ANSI.sub("", raw)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\x08", "")
    return [line for line in raw.split("\n")]



def cmd_logs(project: Project, args) -> int:
    targets = args.services or [s.name for s in project.order(None)]
    files = []
    for name in targets:
        svc = project.services.get(name)
        if not svc:
            print(f"devpad: unknown service {name!r}")
            return 2
        if svc.kind == "docker":
            cmd = compose_cmd(svc) + ["logs", "--tail", str(args.lines or 100)]
            if args.follow:
                cmd.append("-f")
            cmd += [str(s) for s in svc.docker_services]
            try:
                subprocess.run(cmd, cwd=str(svc.dir))
            except KeyboardInterrupt:
                pass
            continue
        files.append(svc.log_path())

    existing = [f for f in files if f.is_file()]
    if not existing:
        print("devpad: no log files yet — has anything been started?")
        return 1
    if args.follow:
        cmd = ["tail", "-n", str(args.lines), "-F", *[str(f) for f in existing]]
        if args.prefix and len(existing) > 1:
            # `tail -F` loses per-file attribution; use a small multiplexer instead.
            return _multiplex(existing, args.lines, project)
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
        return 0
    for f in existing:
        text = _read_log_lines(f)
        tail = text[-args.lines :] if args.lines else text
        if args.prefix and len(existing) > 1:
            print(f"----- {rel(project, f)} -----")
        for line in tail:
            print(_paint(line, f.stem, len(existing) > 1) if args.prefix else line)
    return 0


COLORS = ["\033[36m", "\033[35m", "\033[33m", "\033[32m", "\033[34m", "\033[91m"]


def _paint(line: str, name: str, show: bool) -> str:
    if not show or not sys.stdout.isatty():
        return line
    idx = sum(ord(c) for c in name) % len(COLORS)
    return f"{COLORS[idx]}[{name}]\033[0m {line}"


def _multiplex(files: list[Path], lines: int, project: Project) -> int:
    """Follow several logs in one terminal, each line prefixed by service name."""
    import collections
    import select

    handles = {}
    for f in files:
        try:
            fh = open(f, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        tail = fh.readlines()[-lines:] if lines else []
        for line in tail:
            print(_paint(line.rstrip("\n"), f.stem, True))
        fh.seek(0, os.SEEK_END)
        handles[fh] = f.stem
    if not handles:
        return 1
    print("\033[2m--- following; Ctrl-C to stop ---\033[0m")
    try:
        while True:
            ready, _, _ = select.select(list(handles), [], [], 1.0)
            for fh in ready:
                chunk = fh.readline()
                if chunk == "":
                    continue
                print(_paint(chunk.rstrip("\n"), handles[fh], True), flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        for fh in handles:
            fh.close()


def cmd_attach(project: Project, args) -> int:
    """Start a single service in the foreground: real TTY, real colors, real input."""
    svc = project.services.get(args.service)
    if not svc:
        print(f"devpad: unknown service {args.service!r}")
        return 2
    if svc.kind != "proc":
        print(f"devpad: `attach` only supports kind: proc (got {svc.kind})")
        return 2
    env = os.environ.copy()
    env.update(svc.env)
    print(f"\033[2m--- {svc.name}: {svc.command}  (cwd {rel(project, svc.dir)}) ---\033[0m")
    try:
        proc = subprocess.Popen(["/bin/sh", "-lc", str(svc.command)], cwd=str(svc.dir), env=env)
    except OSError as exc:
        print(f"devpad: spawn failed: {exc}")
        return 127
    write_state(
        svc,
        {
            "name": svc.name,
            "kind": svc.kind,
            "pid": proc.pid,
            "pgid": proc.pid,
            "port": svc.effective_port,
            "dir": str(svc.dir),
            "command": svc.command,
            "started_at": time.time(),
            "log": None,
            "attached": True,
            "exit_code": None,
        },
    )
    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        kill_group(proc.pid, signal.SIGINT)
        rc = proc.wait()
    clear_state(svc)
    return rc


def cmd_exec(project: Project, args) -> int:
    svc = project.services.get(args.service)
    if not svc:
        print(f"devpad: unknown service {args.service!r}")
        return 2
    if svc.kind != "docker":
        print(f"devpad: `exec` targets a docker service (got {svc.kind})")
        return 2
    target = args.target
    if not target:
        target = str(svc.docker_services[0]) if svc.docker_services else None
    if not target:
        print(f"devpad: service {svc.name!r} declares no container names; pass one explicitly")
        return 2
    cmd = compose_cmd(svc) + ["exec", target, *args.command]
    if args.tty:
        cmd.insert(len(compose_cmd(svc)) + 1, "-it")
    try:
        return subprocess.run(cmd, cwd=str(svc.dir)).returncode
    except KeyboardInterrupt:
        return 130


def cmd_ports(project: Project, args) -> int:
    if args.ports:
        ports = [int(p) for p in args.ports]
    else:
        ports = _declared_ports(project)
    if not ports:
        print("devpad: no ports declared in config and none given")
        return 1
    rows = []
    for port in ports:
        pids = port_pids(port)
        if not pids:
            rows.append([str(port), "free", "", ""])
            continue
        for pid in pids:
            rows.append(
                [
                    str(port),
                    "IN USE",
                    str(pid),
                    (pid_command(pid)[:70] + ("…" if len(pid_command(pid)) > 70 else "")),
                ]
            )
    print(_table(["PORT", "STATE", "PID", "COMMAND"], rows))
    if not args.ports:
        print("\nclear a port with:  devpad kill <port>   (or devpad restart <service>)")
    return 0


def _declared_ports(project: Project) -> list[int]:
    seen = []
    for svc in project.order(None):
        p = svc.effective_port
        if p and p not in seen:
            seen.append(int(p))
    return seen


def cmd_kill(project: Project, args) -> int:
    total = 0
    for spec in args.ports:
        port = int(spec)
        pids = port_pids(port)
        if not pids:
            print(f"port {port}: already free")
            continue
        for pid in pids:
            print(f"port {port}: killing pid {pid} ({pid_command(pid)[:60]})")
            kill_group(pid, signal.SIGTERM)
        time.sleep(0.4)
        for pid in port_pids(port):
            print(f"port {port}: pid {pid} survived SIGTERM, sending SIGKILL")
            kill_group(pid, signal.SIGKILL)
        if port_free(port):
            print(f"port {port}: freed")
            total += 1
        else:
            print(f"port {port}: still busy: {port_pids(port)}")
    # tidy state files whose process is gone
    for svc in project.services.values():
        state = read_state(svc)
        if state and state.get("pid") and not pid_alive(int(state["pid"])):
            clear_state(svc)
    return 0 if total or not args.ports else 1


def cmd_rebuild(project: Project, args) -> int:
    """The `nuke and pave` path: stop, clean caches, install deps, rebuild images, start."""
    services = project.order(args.services or None)
    print("== 1/5 stop ==")
    do_stop(list(reversed(services)), down=True)

    print("\n== 2/5 clean ==")
    for svc in services:
        for pattern in svc.clean_globs:
            for target in sorted(svc.dir.glob(pattern)):
                if target.exists() and target.is_dir() and not target.is_symlink():
                    print(f"  - rm -rf {rel(project, target)}")
                    if not args.dry_run:
                        shutil.rmtree(target, ignore_errors=True)
                elif target.is_file():
                    print(f"  - rm {rel(project, target)}")
                    if not args.dry_run:
                        target.unlink(missing_ok=True)
        for cmd in svc.clean:
            print(f"  - {svc.name}: {cmd}")
            if not args.dry_run:
                ok = run_cmd_service(Service(svc.name, dict(svc.raw, command=cmd), project), log=print)
                if not ok and not svc.optional:
                    print(f"devpad: clean step failed for {svc.name}")
                    return 1
    if args.global_clean:
        # Deliberately does nothing yet: this flag is reserved for heavier cache
        # wipes. devpad never deletes a directory the config did not name.
        print("  (--global-clean is reserved; declare clean_globs instead)")

    print("\n== 3/5 install ==")
    for svc in services:
        for cmd in svc.install:
            print(f"  - {svc.name}: {cmd}")
            if not args.dry_run:
                ok = run_cmd_service(Service(svc.name, dict(svc.raw, command=cmd), project), log=print)
                if not ok and not svc.optional:
                    print(f"devpad: install step failed for {svc.name}")
                    return 1

    print("\n== 4/5 build ==")
    for svc in services:
        for cmd in svc.build:
            print(f"  - {svc.name}: {cmd}")
            if not args.dry_run:
                ok = run_cmd_service(Service(svc.name, dict(svc.raw, command=cmd), project), log=print)
                if not ok and not svc.optional:
                    print(f"devpad: build step failed for {svc.name}")
                    return 1

    print("\n== 5/5 start ==")
    if args.dry_run:
        print("(dry run: not starting)")
        return 0
    ok = do_start(services, force=True)
    print("")
    cmd_status(project, args, quiet=False)
    return 0 if bool(ok) else 1


def resolve_binary(token: str, cwd: Path) -> str | None:
    """Locate a command's executable the way a shell would, from `cwd`.

    `shutil.which` alone is wrong for a relative program such as
    `./.venv/bin/python`: it does not resolve paths against the working directory,
    so doctor would report a healthy venv as missing.
    """
    if not token:
        return None
    if "/" in token or os.sep in token:
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        if sys.platform == "win32" and candidate.with_suffix(".exe").is_file():
            return str(candidate.with_suffix(".exe"))
        return None
    return shutil.which(token)


def _doctor_drivers(warn, bad, ok):
    """Report which window drivers work, because "no window appeared" is otherwise
    indistinguishable from "the service failed"."""
    try:
        term = _terminals()
    except ConfigError as exc:
        bad(f"terminal drivers unavailable: {exc}")
        return
    print("terminal drivers (for `devpad up --terminal`):")
    for row in term.report_drivers():
        name = row["driver"]
        if row["available"]:
            note = "" if row["can_close"] else " (cannot close its own window)"
            ok(f"{name}: {row['detail']}{note}")
        else:
            warn(f"{name}: {row['detail']}")
    driver, why = term.resolve_driver("auto")
    if driver is None:
        bad(f"auto driver would fail: {why}")
    else:
        print(f"  \033[32m->\033[0m auto would use: {why}")


def cmd_doctor(project: Project, args) -> int:
    problems = []
    warns = []

    def ok(msg):
        print(f"  \033[32mok\033[0m   {msg}")

    def warn(msg):
        warns.append(msg)
        print(f"  \033[33mwarn\033[0m {msg}")

    def bad(msg):
        problems.append(msg)
        print(f"  \033[31mFAIL\033[0m {msg}")

    print(f"config: {rel(project, project.config_path)}  (root {project.root})")
    if project.draft:
        print("  \033[33mNOTE\033[0m this is a generated draft (`draft: true`): fill in the marked")
        print("       services and remove that key once the config is yours.")
    print(f"python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"os:     {sys.platform}")
    print("")

    needs_docker = any(s.kind == "docker" for s in project.services.values())
    if needs_docker:
        if not docker_available():
            bad("docker CLI not found but a docker service is declared")
        elif not docker_daemon_up():
            bad("docker daemon not reachable — start Docker Desktop")
        else:
            ok(f"docker reachable ({run_capture(['docker', 'version', '--format', '{{.Server.Version}}']).strip()})")
        for svc in project.services.values():
            if svc.kind == "docker":
                for f in svc.docker_files:
                    (ok if (svc.dir / f).is_file() else bad)(f"{svc.name}: compose file {f} " + ("found" if (svc.dir / f).is_file() else "MISSING"))

    for svc in project.order(None):
        label = f"{svc.name} ({svc.kind})"
        if svc.kind == "cmd":
            ok(f"{label}: one-shot step")
            continue
        if not svc.dir.is_dir():
            bad(f"{label}: dir does not exist -> {svc.dir}")
            continue
        if svc.kind == "proc":
            binary = _first_token(svc.resolved_command() or "")
            found = resolve_binary(binary, svc.dir)
            if not found:
                bad(f"{label}: `{binary}` not found (looked in {svc.dir} and on PATH)")
            else:
                ok(f"{label}: {binary} -> {found}")
        port = svc.effective_port
        if port:
            holders = port_pids(int(port))
            if holders:
                warn(f"{label}: port {port} already in use by pid {holders} ({pid_command(holders[0])[:50]})")
            else:
                ok(f"{label}: port {port} free")

    for tool, why in (("node", "frontend tooling"), ("pnpm", "frontend package manager"), ("adb", "android device bridge")):
        if shutil.which(tool):
            ok(f"{tool} present")
        elif args.strict:
            bad(f"{tool} missing ({why})")
        else:
            warn(f"{tool} missing ({why}) — only matters if you use it")

    print("")
    _doctor_drivers(warn, bad, ok)

    print("")
    if problems:
        print(f"\033[31m{len(problems)} problem(s)\033[0m, {len(warns)} warning(s)")
        return 1
    print(f"\033[32mall checks passed\033[0m ({len(warns)} warning(s))")
    return 0


def _first_token(command: str) -> str:
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.split()
    for tok in toks:
        if "=" in tok and not tok.startswith("/") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tok):
            continue
        return tok
    return ""


def _template_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "devpad.example.yaml"


def cmd_init(project_dir: Path, args) -> int:
    target = Path(args.path) if args.path else project_dir / "devpad.yaml"
    if target.exists() and not args.force:
        print(f"devpad: {target} already exists (use --force to overwrite)")
        return 1

    if args.detect:
        try:
            detect = _load_detector()
        except ImportError as exc:
            print(f"devpad: cannot load detect.py ({exc}); falling back to the starter template")
        else:
            result = detect.detect(project_dir)
            print(detect.render_report(result))
            text = detect.render_yaml(result)
            if args.show:
                print("GENERATED CONFIG\n")
                print(text)
                return 0 if not result.unknowns else 1
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"wrote {target}")
            print("")
            if result.unknowns or not result.findings:
                print("Anything marked LIKELY / GUESS still needs your confirmation.")
                print("Fix those, then: devpad doctor")
                return 1
            print("Nothing was inferred. Next: devpad doctor && devpad up")
            return 0

    template = _template_path().read_text(encoding="utf-8")
    if args.show:
        print(template)
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template, encoding="utf-8")
    print(f"wrote {target}")
    print("edit the service definitions, then: devpad doctor && devpad up")
    return 0


def _load_detector():
    """Import scripts/detect.py from the same directory as this file."""
    import importlib.util

    path = Path(__file__).resolve().parent / "detect.py"
    if not path.is_file():
        raise ImportError(f"{path} not found")
    spec = importlib.util.spec_from_file_location("devpad_detect", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_clean(project: Project, args) -> int:
    """Remove runtime state and logs (does not touch your source tree)."""
    removed = []
    if project.logs_dir.is_dir():
        for f in project.logs_dir.glob("*.log"):
            removed.append(rel(project, f))
            f.unlink()
    if project.run_dir.is_dir():
        for f in project.run_dir.glob("*.json"):
            removed.append(rel(project, f))
            f.unlink()
    if args.all:
        for pattern in ("cache", "tmp"):
            p = project.runtime_dir / pattern
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed.append(rel(project, p))
    print(f"removed {len(removed)} file(s)" if removed else "nothing to clean")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def add_terminal_flag(parser):
    parser.add_argument(
        "-t",
        "--terminal",
        action="store_true",
        help="run each proc service in its own visible terminal window (log stays on screen)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="devpad",
        description="Restart-and-inspect local dev services (frontend / backend / docker) without long commands.",
        epilog="Services are defined in devpad.yaml. Run `devpad init --detect` to generate one.",
    )
    p.add_argument("--version", action="version", version=f"devpad {VERSION}")
    p.add_argument("-C", "--dir", help="run as if started in this directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_, aliases=()):
        s = sub.add_parser(name, help=help_, aliases=list(aliases))
        s.add_argument("services", nargs="*", help="service names (default: all)")
        return s

    s = add("up", "start services in dependency order")
    s.add_argument("-f", "--force", action="store_true", help="restart procs and rebuild images")
    s.add_argument("--attach", action="store_true", help="follow logs after starting")
    add_terminal_flag(s)

    s = add("down", "stop everything (containers are brought down)", aliases=["stop-all"])
    s.add_argument("--keep-containers", action="store_true", help="`compose stop` instead of `compose down`")

    s = add("start", "start services without restarting the ones already up")
    add_terminal_flag(s)
    s = add("stop", "stop services (containers are stopped, not removed)")
    s = add("restart", "stop then start (the common case)")
    s.add_argument("-f", "--force", action="store_true", help="also rebuild images")
    add_terminal_flag(s)

    s = add("status", "show what is running, on which port, since when", aliases=["ps"])
    s.add_argument("--json", action="store_true", help="machine readable output")

    s = add("logs", "print or follow service output")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", "--lines", type=int, default=100)
    s.add_argument("--no-prefix", dest="prefix", action="store_false", default=True)

    s = sub.add_parser("attach", help="run one proc service in this terminal (real TTY, colors, input)")
    s.add_argument("service", help="service name")
    s.add_argument("-n", "--lines", type=int, default=0)

    s = sub.add_parser("exec", help="run a command inside a docker service's container")
    s.add_argument("-t", "--tty", action="store_true", help="allocate a TTY (for shells)")
    s.add_argument("service")
    # REMAINDER keeps `-uroot -p` and other container flags out of devpad's own
    # option parsing; normalise_exec_argv moves `-t` ahead of the service name.
    s.add_argument("command", nargs=argparse.REMAINDER)

    s = sub.add_parser("ports", help="show which processes hold the declared ports")
    s.add_argument("ports", nargs="*", type=int)

    s = sub.add_parser("kill", help="free a TCP port by killing whatever holds it")
    s.add_argument("ports", nargs="+", type=int)

    s = add("rebuild", "stop, clean caches, install, build, start")
    s.add_argument("--dry-run", action="store_true", help="show the plan without running it")
    s.add_argument("--global-clean", action="store_true", help="reserved for heavier cache wipes")

    s = add("doctor", "check tooling, paths, ports and docker before you blame the code")
    s.add_argument("--strict", action="store_true", help="treat missing optional tools as failures")

    s = add("clean", "delete runtime state + logs (never touches source)")
    s.add_argument("-a", "--all", action="store_true", help="also drop cached artifacts under the runtime dir")

    s = sub.add_parser("init", help="write a starter devpad.yaml")
    s.add_argument("path", nargs="?")
    s.add_argument("-f", "--force", action="store_true")
    s.add_argument(
        "--detect",
        action="store_true",
        help="inspect this project and generate the config from what is actually there",
    )
    s.add_argument("--print", dest="show", action="store_true", help="print instead of writing")

    s = sub.add_parser("config", help="print the resolved project + services")
    return p


def normalise_exec_argv(argv: list[str]) -> list[str]:
    """Move exec's `-t`/`--tty` to just after the subcommand.

    `devpad exec infra mysql -uroot -p` must hand `-uroot -p` to the container
    untouched, while `devpad exec infra sh -t` should still allocate a TTY. Since
    argparse cannot do both, the flag is hoisted before the service name and the
    remainder is left alone. A literal `--` always ends the option region.
    """
    try:
        idx = next(i for i, a in enumerate(argv) if a == "exec" or a.endswith("/exec"))
    except StopIteration:
        return argv
    head, tail = argv[: idx + 1], argv[idx + 1 :]
    prefix: list[str] = []
    rest: list[str] = []
    seen_service = False
    literal = False
    for tok in tail:
        if literal:
            rest.append(tok)
            continue
        if tok == "--":
            literal = True
            continue
        if not seen_service:
            if tok in ("-t", "--tty"):
                prefix.append(tok)
                continue
            if tok.startswith("-") and tok != "-":
                prefix.append(tok)  # help/version: let argparse report it
                continue
            seen_service = True
            rest.append(tok)
            continue
        rest.append(tok)
    return head + prefix + rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalise_exec_argv(list(sys.argv[1:] if argv is None else argv)))

    start = Path(args.dir).expanduser().resolve() if getattr(args, "dir", None) else Path.cwd()

    if args.cmd == "init":
        return cmd_init(start, args)

    try:
        config_path = find_config(start)
        data = load_config_file(config_path)
        project = Project(config_path, data)
    except ConfigError as exc:
        print(f"devpad: {exc}")
        return 2
    except FileNotFoundError as exc:
        print(f"devpad: {exc}")
        return 2

    handlers = {
        "up": cmd_up,
        "down": cmd_down,
        "stop-all": cmd_down,
        "start": lambda p, a: _start_only(p, a),
        "stop": lambda p, a: _stop_only(p, a),
        "restart": cmd_restart,
        "status": cmd_status,
        "ps": cmd_status,
        "logs": cmd_logs,
        "attach": cmd_attach,
        "exec": cmd_exec,
        "ports": cmd_ports,
        "kill": cmd_kill,
        "rebuild": cmd_rebuild,
        "doctor": cmd_doctor,
        "clean": cmd_clean,
        "config": cmd_config,
    }
    if args.cmd == "status" and getattr(args, "json", False):
        return cmd_status_json(project)
    try:
        return handlers[args.cmd](project, args)
    except ConfigError as exc:
        print(f"devpad: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\ndevpad: interrupted")
        return 130


def _start_only(project: Project, args) -> int:
    services = project.order(args.services or None)
    ok = do_start(services, terminal=getattr(args, "terminal", False))
    print("")
    cmd_status(project, args, quiet=False)
    return 0 if bool(ok) else 1


def _stop_only(project: Project, args) -> int:
    services = project.order(args.services or None)
    ok = do_stop(list(reversed(services)), down=args.cmd == "down")
    print("")
    cmd_status(project, args, quiet=False)
    return 0 if bool(ok) else 1


def cmd_status_json(project: Project) -> int:
    out = []
    for svc in project.order(None):
        state = read_state(svc) or {}
        out.append(
            {
                "name": svc.name,
                "kind": svc.kind,
                "running": svc_running(svc),
                "pid": state.get("pid"),
                "port": svc.effective_port,
                "started_at": state.get("started_at"),
                "dir": str(svc.dir),
                "command": svc.resolved_command(),
                "log": str(svc.log_path()),
                "depends_on": svc.depends_on,
            }
        )
    print(json.dumps(out, indent=2))
    return 0


def cmd_config(project: Project, args) -> int:
    print(f"config  {project.config_path}")
    print(f"root    {project.root}")
    print(f"runtime {project.runtime_dir}")
    print("")
    rows = []
    for svc in project.order(None):
        rows.append(
            [
                svc.name,
                svc.kind,
                svc.resolved_command() or " ".join(svc.docker_services) or "-",
                str(svc.effective_port or "-"),
                ",".join(svc.depends_on) or "-",
            ]
        )
    print(_table(["SERVICE", "KIND", "COMMAND", "PORT", "DEPENDS"], rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
