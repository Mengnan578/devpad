# devpad reference

Design rationale, the full config schema, docker patterns, and the install path.

---

## 1. Why a tool and not a folder of scripts

The honest answer is that scripts win for exactly one service and lose as soon as
there are three, because the hard parts are not the commands — they are the
bookkeeping around them.

| Concern | Scripts | devpad |
|---|---|---|
| Change a path or port | edit every script that mentions it | edit one config line |
| Restart one service | either N scripts or a hand-typed line | `devpad restart api` |
| Start order (db → api → metro) | `sleep 5`, and it still breaks on a cold docker pull | declared `depends_on` + real readiness probes |
| Readiness | guessed | `http` / `tcp` / `port` / `cmd` probe per service, with timeout |
| Stopping | `kill $PID` leaves `npx`/`uvicorn --reload` children behind, so the port stays busy | process-group signal, then SIGKILL, then a port sweep |
| "Why is 8081 taken?" | manual `lsof` archaeology | `devpad ports` / `devpad kill 8081` |
| "What did it print before it died?" | whatever is still scrolled back in one of six terminals | per-service log files with a start banner |
| A config mistake | fails halfway through, in the wrong directory | `devpad doctor` before anything starts |
| Someone else picks it up | reads four scripts | reads one file with a documented schema |

The one real advantage of a pile of scripts is zero ceremony. devpad keeps that:
it is a single Python file with no dependencies, and any shell command you would
have written goes into a `command:` field verbatim. You lose the ceremony, not
the flexibility.

The two places a tool must be *carefully* better than a script — because both
cause the "I restarted it and now it is worse" failure mode — are:

- **Ownership.** `devpad` records the pid *and* process-group id of what it
  started, and proves ownership two independent ways before signalling it: the
  pgid is the one it created, or the executable identity still matches. Neither
  alone is sufficient — `npx react-native start` hides behind `sh`, while a bare
  interpreter is recognisable but easily confused with another Python process.
  When ownership cannot be proven, devpad refuses to guess and tells you which
  process holds the port instead of killing a stranger.
- **Idempotence.** `up` on a running stack is a no-op, not a double-bind on the
  port; `restart` on a stopped service just starts it. Repeated commands during a
  debugging session are the normal case, not the exception.

---

## 2. Install

Nothing to install for the tool itself (Python 3.8+, stdlib only). PyYAML is used
if importable; otherwise a built-in parser handles standard YAML.

### Option A — call it by path (nothing to set up)

```bash
python3 /path/to/devpad/scripts/devpad.py status
```

### Option B — a shim on your `PATH` (one line)

```bash
sudo tee /usr/local/bin/devpad >/dev/null <<'EOF'
#!/bin/sh
exec python3 /absolute/path/to/devpad/scripts/devpad.py "$@"
EOF
sudo chmod +x /usr/local/bin/devpad
```

Then `devpad status` works from anywhere inside the project. If you would rather
not touch `/usr/local/bin`, put the same file in `~/.local/bin` (add it to `PATH`)
or drop an alias in your shell rc.

### Option C — per project, checked in

```bash
ln -s ../devpad/scripts/devpad.py ./dev
./dev restart api
```

Committing `devpad.yaml` lets every teammate and every agent use the same short
commands with no path memorisation. **Do not commit `.devpad/`** — add it to
`.gitignore`; it holds only pids and logs.

### Config discovery

`devpad` looks for, in order: `devpad.yaml`, `devpad.yml`, `.devpad/config.yaml`,
`.devpad/config.yml`, `devpad.json`, walking up from the current directory. The
first hit wins, so running from a subdirectory behaves the same as from the root.
`DEVPAD_CONFIG=/abs/path.yaml` overrides discovery; `-C DIR` changes the starting
directory.

### Generating the config from the project (`--detect`)

`devpad init --detect` runs `scripts/detect.py`, which reads the project rather
than asking questions. Its central rule is that **a fact and an inference are
never presented the same way**:

| Confidence | Meaning | Where it comes from |
|---|---|---|
| (unmarked) | read from a project file | `package.json`, lockfile, `requirements.txt`, `pyproject.toml`, `manage.py`, compose file |
| `LIKELY` | inferred from strong evidence | uvicorn target found by locating `app = FastAPI()` |
| `GUESS` | needs confirmation | Flask factory app whose module path cannot be determined |

Anything the detector cannot decide is emitted as an `unknown` and surfaces under
`NEEDS A DECISION` — a Flask factory, a missing `poetry`/`uv` CLI, a compose file
with no published host port, several compose files, or a `package.json` with no
start script. `devpad init --detect` then exits **1** so a caller can branch, and
`render_yaml` marks the file `draft: true`. Delete that key once the file is yours;
it exists only to let `devpad doctor` read a not-yet-finished config.

A fully certain detection carries no `draft` key and needs no editing — it is
immediately runnable. An empty detection is a valid answer: no service is invented,
and the draft carries a commented placeholder plus the reason.

Exit codes overall: `0` success, `1` a required service failed / a decision is
pending, `2` invalid config or arguments.

`.devpad/` is created next to the config and contains:

```
.devpad/run/<service>.json    pid, pgid, port, command, start time, log path
.devpad/logs/<service>.log    that service's combined stdout+stderr
```

---

## 3. Full schema

```yaml
version: 1
draft: true              # optional; set by `init --detect`. Allows an empty or
                         # unfinished `services` map. Remove it when the config is yours.
defaults: { ... }        # merged into every service; per-service keys win
services:
  <name>:
    kind: proc | docker | cmd
```

### Common fields

| Field | Type | Meaning |
|---|---|---|
| `kind` | `proc` \| `docker` \| `cmd` | Required. Long-running process, compose group, one-shot command. |
| `description` | string | Shown in `status`. Cheap documentation. |
| `dir` | path | Working directory, relative to the config file. Default `.`. |
| `depends_on` | list | Start after these, in this order. Also pulls them into a targeted `restart`. |
| `port` | int or name | The port you would reach it on. Named shortcuts: `metro`→8081, `api`/`server`→8080, `web`/`vite`→5173, `db`/`mysql`→3306, `postgres`→5432, `redis`→6379, `mongo`→27017. Used for the pre-flight conflict check, for cleanup after stop, and for display. |
| `ready` | map or string | Readiness probe. See below. |
| `optional` | bool | `true` = a failure logs a warning and does not fail the run or block dependents. Use for `worker`, simulators, anything flaky. || `env` | map | Extra environment for the child, on top of the parent's. |
| `stop_timeout` | number | Seconds between SIGTERM and SIGKILL. Default 10. |
| `stop_signal` | signal name | Default `SIGTERM`. Some servers prefer `SIGINT`. |
| `flags` | list | Docker only: `build`, `force-recreate`. |

### `kind: proc`

| Field | Meaning |
|---|---|
| `command` | Required. A shell line, run via `/bin/sh -lc` in `dir`. Anything you would type is valid — pipes, `&&`, env prefixes, `${VAR}` from `env`. |
| `stop_command` | Optional extra command run before signalling (e.g. `adb reverse --remove-all`). |

Two ways to run it:

- **Managed (default)** — detached, output to `.devpad/logs/<name>.log`, survivable
  across the agent's or your own shell exiting, stoppable as a group.
- **`devpad attach <name>`** — in your terminal, with a real TTY. Not tracked as a
  managed process (it dies with your terminal, which is what you want for hotkeys
  and interactive prompts).

### `kind: docker`

| Field | Meaning |
|---|---|
| `compose` | Compose file name(s), relative to `dir`. Accepts a list. |
| `services` | Which compose services to operate on. Empty = all of them. |
| `project_name` | Compose project name (`-p`). Set it when `dir` is not unique. |
| `build` | Pre-build commands run by `devpad rebuild` (e.g. `docker build -t app:dev .`). |

`up` runs `docker compose up -d [--build] <services>`; `stop` runs `compose stop`;
`down` runs `compose down`. `ready` cannot inspect container internals from the
host, so probe the **published host port** (`tcp: 3306`) or use a `cmd` probe such
as `docker compose exec -T db pg_isready -U postgres`.

### `kind: cmd`

One-shot: `migrate`, `pod install`, `adb reverse`, `docker build`. Runs in the
foreground when a `start`/`up`/`rebuild` reaches it; non-zero exit fails the run
unless `optional: true`. Not tracked in `status` (there is nothing to track), so
it is safe to re-run.

### `ready` probes

| Form | Meaning |
|---|---|
| `ready: { http: "http://127.0.0.1:8080/health", timeout: 90 }` | Poll an HTTP GET; any status < 500 counts as up. |
| `ready: { tcp: 5432, timeout: 120 }` | Poll a TCP connect. |
| `ready: { port: 8081 }` | Same as tcp, expressed as "the declared port". |
| `ready: { cmd: "pg_isready -h 127.0.0.1" }` | Poll a shell command; exit 0 = up. |
| `ready: "http://…/health"` | Shorthand for an HTTP probe. |
| `ready: { timeout: 600 }` | **No probe** — start it and move on. For long builds. |
| omitted | Defaults to the `port` if one is declared, otherwise no probe. |

A probe detects "process is up but not serving" instead of letting dependents fail
against a socket that is not listening yet. It also notices the process **dying**
during the wait and reports the exit immediately rather than burning the timeout.

### `rebuild` steps

`devpad rebuild [svc...]` runs, per service, in this order:

1. **stop** — everything in the selected set, reverse dependency order.
2. **clean** — `clean_globs` (deleted with `shutil.rmtree`/`unlink`) then `clean` (shell lines).
3. **install** — `install` shell lines (`pnpm install`, `pip install -r …`).
4. **build** — `build` shell lines (`pod install`, `docker build`, `codegen`).
5. **start** — `up -f` for the set.

`--dry-run` prints the plan without executing anything — always worth running
first, because step 2 deletes files. Anything not declared is left alone: devpad
does not guess that `.venv` or `node_modules` should be deleted. If you want
`node_modules` gone, say so: `clean_globs: ["node_modules"]`.

---

## 4. Docker patterns for this stack

### Databases and middleware (the common case)

```yaml
infra:
  kind: docker
  dir: ./infra
  compose: docker-compose.yml
  services: [mysql, redis]
  ready: { tcp: 3306, timeout: 120 }
```

Docker Desktop on macOS binds published ports on the host, so `tcp: 3306` is a
valid readiness signal. A cold start that must pull images can take minutes —
set `timeout` accordingly, or the probe will report "not ready" while the pull is
still running.

### Several compose files in one project

Use two services so they can be restarted independently:

```yaml
infra:
  kind: docker
  dir: ./infra
  compose: docker-compose.yml
  services: [mysql, redis]
db-tools:
  kind: docker
  dir: ./tools
  compose: docker-compose.yml
  project_name: tools
  services: [adminer]
  optional: true
```

Set `project_name` whenever two compose files share a directory, otherwise Docker
Compose derives the same project name for both and they collide.

### A containerised Android emulator

```yaml
emulator:
  kind: docker
  dir: ./infra
  compose: docker-compose.yml
  services: [android-emulator]
  depends_on: [infra]
  ready: { cmd: "docker compose exec -T android-emulator adb shell getprop sys.boot_completed | grep -q 1", timeout: 300 }
  optional: true
```

Only meaningful on a Linux host with KVM. On macOS use a native AVD
(`emulator -avd Pixel_7`) and connect Metro with
`adb reverse tcp:8081 tcp:8081`, which is a natural `kind: cmd` service.

### Entering a container

```bash
devpad exec infra mysql -uroot -p     # exec into the first declared service
devpad exec -t infra sh               # -t before the service name allocates a TTY;
                                      # everything after the name reaches the container verbatim
```

---

## 4b. Terminal windows (`--terminal`)

`devpad up --terminal` starts each `proc` service in its own visible window instead
of detaching it with output redirected to a file. That is the primary mode: the log
has to be somewhere the user can scroll, select and copy.

### Driver matrix

`devpad doctor` prints this for your machine, and `driver: auto` walks it in order.

| Driver | Mechanism | Close own window | Needs Automation permission |
|---|---|---|---|
| `iterm` | AppleScript `create window` + `write text` | yes — by session tty | yes, prompted once |
| `apple-terminal` | AppleScript `do script` | yes — by custom title | yes, prompted once |
| `tmux` | detached session, then attach a window | yes — by session name | no |
| `command-file` | writes a `.command`, runs it via `open(1)` | **no** | **no** |
| `direct` | runs in the current terminal, no window | n/a | no |

`auto` order: `iterm` → `apple-terminal` → `tmux` → `command-file`. `direct` is only
used when named explicitly, because it does not open a window at all.

### Implementation constraints (each one was a real failure)

1. **`write text` must wait for the session to be idle.** Issued immediately after
   `create window`, the command is silently dropped: the window opens, does nothing,
   and vanishes. The driver polls `is processing of session = false` first.
2. **iTerm has no usable `sessions of window` collection** (`-1728`), and `id of
   window` is not dependable. Sessions are reached via `current session of window i`,
   each access wrapped in `try` — some windows have no current session and would
   otherwise abort the whole script.
3. **Terminal.app lives in `/System/Applications/Utilities/`.** Checking only
   `/Applications` and `/System/Applications` reports it as not installed.
4. **The service runs through a login shell** (`$SHELL -lc`). A window opened from an
   app icon does not inherit the profile, so `node` from `nvm` or `python3` from
   `conda` would otherwise be the wrong one or missing.

### The generated runner

Each window executes a wrapper in `.devpad/terminal/<name>.command`:

```sh
#!/bin/sh
DEVPAD_TITLE="myapp · api"
cd /path/to/server || exit 1
printf '%s\n' "$$" > .devpad/pids/api.pid
"$SHELL" -lc 'uvicorn app.main:app --reload --port 8080'
devpad_code=$?
printf '\n── %s exited with code %s ──\n' "$DEVPAD_TITLE" "$devpad_code"
printf 'Press Enter to close this window... '
read -r _
```

- the pid is written first, so devpad can track a process it did not spawn;
- the exit code is printed, so a crash is legible;
- `read` holds the window, so the log survives the process.

### Lifecycle

`down` and `restart` handle windowed services differently from detached ones, in
this order: **SIGTERM the process group → wait `stop_timeout` → SIGKILL → close the
window**. Closing first would leave a process holding the port with nothing on screen
to explain it — the orphan that makes the next start fail with "address already in
use".

`reuse: true` in the terminal block is reserved for reusing a window per service
rather than creating a new one; the default is a fresh window per start, which keeps
scrollback honest.

### Per-service control

```yaml
terminal: false              # never give this service a window (databases, one-shots)
terminal: { hold: false }    # close the window when the process exits cleanly
terminal: { title: "API", driver: tmux }
```

A service with `terminal: false` still runs and is still managed; it just logs to
`.devpad/logs/<name>.log` instead of owning a window.

### When windows are not possible

If the host cannot send Apple Events (a sandboxed agent, for example), windowed mode
cannot work from that host. Options, in order:

1. run `devpad up --terminal` in your own terminal;
2. set `terminal.driver: command-file` — no Automation permission, though devpad then
   cannot close the window for you;
3. grant the host permission under System Settings → Privacy & Security → Automation.

`devpad doctor` states which drivers work and why the others do not.

---

## 5. Workflows

### The everyday loop

```bash
devpad status                 # orient: what is actually up?
devpad restart api            # change backend code
devpad logs api -n 50         # confirm it came back clean
```

### Frontend changes that need a Metro cache reset

There is no flag for "wipe Metro's cache" — flags that reset state should be
visible in the config, not hidden in a command you have to remember:

```yaml
app:
  command: npx react-native start --reset-cache
```

`devpad restart app -f` exists for the other case: it passes `--build` to a docker
service in the set, and restarts a proc even if devpad thinks it is up.

### Something is wrong and you do not know what

```bash
devpad doctor                 # environment / paths / ports / docker
devpad ports                  # who owns each declared port
devpad logs api app -n 100    # last 100 lines of both, in one pass
```

### Recovering from a genuinely broken state

```bash
devpad down
devpad kill 8081 8080         # only if something is still holding ports
devpad rebuild --dry-run      # read the plan
devpad rebuild
```

### Using it from an agent

`devpad status --json` gives structured truth (running, pid, port, uptime,
command, log path) so an agent can decide without parsing a table. `devpad logs
<svc> -n N` gives the evidence. `devpad restart <svc>` is the single action. This
is the intended division: the agent reads state and logs, and performs narrow
restarts, instead of reconstructing long shell commands from memory.

---

## 6. Troubleshooting the tool itself

| Symptom | Cause and fix |
|---|---|
| `no devpad config found` | No `devpad.yaml` in this tree. `devpad init`, or set `DEVPAD_CONFIG`. |
| `service 'x' depends on unknown service 'y'` | Typo in `depends_on`. |
| `dependency cycle` | Two services depend on each other. Break it; there is no correct order. |
| Service shows `down` but the port is busy | Something not started by devpad holds it. `devpad ports`, then confirm before `devpad kill`. |
| `recorded pid N is not this service … forgetting it` | Pid reuse after a crash. Self-healing; the next start re-records. |
| `!! not ready after Ns` | The process started but never answered. Read its log; usually a wrong port, a missing dependency, or a DB that is not up. |
| Ready in the log, but devpad says not ready | devpad probes both `127.0.0.1` and `[::1]`, so this is not an IPv4/IPv6 mismatch. Check that the port in `ready` matches the one the server actually bound, and that it is not bound to a container IP only. |
| `!! docker daemon is not reachable` | Docker Desktop is not running. |
| Port already in use right after `stop` | A child escaped the group (a wrapper double-forking). `devpad kill <port>` clears it; consider `stop_timeout` or a `stop_command`. |
| Config changes seem ignored | Confirms `devpad config` prints the file you are editing — discovery may have picked another one higher up. |
| YAML error mentioning a column | A plain scalar contains `: `. Quote it: `command: "python x.py --flag a:b"`. |
