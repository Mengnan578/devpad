# devpad

**Stop losing your dev-server logs. One command opens a terminal per service and starts your stack there.**

React Native / Metro · Python backends · docker compose · macOS

## Install

### Claude Code — plugin marketplace

```text
/plugin marketplace add Mengnan578/devpad
/plugin install devpad
```

No global command, no npm, no Python setup: the plugin carries its own engine and
the skill runs it from `${CLAUDE_PLUGIN_ROOT}`.

### Any other agent, or no agent

```bash
git clone https://github.com/Mengnan578/devpad.git ~/devtools/devpad
cd ~/devtools/devpad && ./install.sh --cli
```

`install.sh` links the skill into every agent skills directory it finds
(`~/.dsh/skills`, `~/.claude/skills`, `~/.agents/skills`) and, with `--cli`, puts a
`devpad` command in `~/.local/bin`.

### npm

Not published yet — `devpad` on npm needs 2FA on every first publish. The plugin and
`install.sh` paths above do not, and neither needs Node at all.

---

## The problem this solves

You restart the frontend and backend dozens of times a day. The commands are long.
And when an AI or a background process starts them for you, **no terminal appears**,
so the logs you actually need scroll by in a buffer you cannot search, select, or
copy.

devpad puts each service in **its own real terminal window**, keeps the window open
when the process dies, and lets you restart **one** service without touching the
others.

```
┌─ myapp · api ────────────────┐   ┌─ myapp · app ────────────────┐
│ INFO:     Uvicorn running    │   │ Metro waiting on :8081        │
│ INFO:     Application startup│   │ bundling... tick 42           │
│ ...                          │   │ ...                           │
└──────────────────────────────┘   └───────────────────────────────┘
```

---

## Quick start

```bash
cd your-project
devpad init --detect         # reads package.json / Python manifest / compose
devpad doctor                # checks tooling, ports, docker and terminal drivers
devpad up --terminal         # one window per service
```

Installed as a Claude Code plugin instead? The skill already knows how to call the
bundled engine — just ask: *"start my project in terminal windows"*.

The detector is explicit about what it knows:

| Marker | Meaning | Based on |
|---|---|---|
| *(none)* | read from a project file | `package.json`, lockfile, `requirements.txt`, `pyproject.toml`, `manage.py`, compose |
| `LIKELY` | inferred from strong evidence | uvicorn target located via `app = FastAPI()` |
| `GUESS` | must be confirmed | Flask factory app, ambiguous module path |

Anything it cannot decide becomes a `NEEDS A DECISION` entry and the command exits
non-zero. It would rather ask than guess — a wrong start command is worse than none.

---

## Commands

```bash
devpad up --terminal          # start everything, one window each
devpad up                     # detached, logs in .devpad/logs (CI-friendly)
devpad restart api --terminal # restart ONLY the backend; reopen only its window
devpad down                   # stop processes, then close the windows it opened
devpad status                 # state, pid, uptime, port, health, window, kind
devpad logs -f api            # follow captured output
devpad attach api             # run one service in your current terminal (real TTY)
devpad doctor                 # environment + terminal-driver matrix
devpad ports                  # who holds the declared ports
devpad kill 8081              # free a port
devpad rebuild api --dry-run  # stop → clean → install → build → start, preview first
devpad detect --json          # machine-readable project recon
```

---

## Why a CLI and not a plugin

| | Plugin | Skill alone | **devpad CLI** |
|---|---|---|---|
| Can guarantee a window opens | only in its own host | no | **yes** |
| Works with any agent / editor | no | yes | **yes** |
| Works with no agent at all | no | no | **yes** |
| Shareable | host-specific | host-specific | **npm** |

Opening a window is an OS action. A skill is prompt-level instruction and cannot
guarantee OS behaviour; a plugin locks you to one host. So the engine is a CLI, and a
thin skill tells an agent when to call it.

---

## Terminal drivers

`auto` picks the best available, and `devpad doctor` shows the matrix.

| Driver | Mechanism | Can close its window | Needs Automation permission |
|---|---|---|---|
| `iterm` | AppleScript | yes (by session tty) | yes, prompt on first use |
| `apple-terminal` | AppleScript `do script` | yes (by custom title) | yes, prompt on first use |
| `tmux` | detached session + attach | yes (by session name) | no |
| `command-file` | `.command` via `open(1)` | **no** | **no** |
| `direct` | runs in the current terminal | n/a | no |

If your host cannot send Apple Events, use `command-file` — it still opens a window
and runs the service; only automatic closing is unavailable. devpad says so instead
of pretending.

### First run may ask for permission

macOS asks once: *"allow <your terminal> to control iTerm2"*. That is expected.
If it is denied, devpad reports the permission hint and falls back — it does not
silently do nothing.

> **Running devpad from inside an AI agent:** the agent host must be allowed to send
> Apple Events. If it is sandboxed against that, windowed mode cannot work from
> there — run `devpad up --terminal` in your own terminal, or set
> `terminal.driver: command-file`.

---

## Why the logs survive

Every window runs a small generated wrapper, not your command directly:

```sh
#!/bin/sh
DEVPAD_TITLE="myapp · api"
cd /path/to/server || exit 1
"$SHELL" -lc 'uvicorn app.main:app --reload --port 8080'
devpad_code=$?
printf '\n── %s exited with code %s ──\n' "$DEVPAD_TITLE" "$devpad_code"
printf 'Press Enter to close this window... '
read -r _
```

Three consequences, each of which is a deliberate decision:

1. **The exit code is printed.** You can see whether it was `0` or a crash.
2. **The window stays open.** A crashed service's last lines are exactly what you
   need; closing the window would delete the evidence.
3. **It runs through your login shell.** A window opened from an app icon does not
   inherit your profile, so without `-lc` the `node` from `nvm` or the `python3`
   from `conda` would be the wrong one — or missing entirely.

---

## Lifecycle guarantees

- **One service, one window.** `restart api --terminal` closes and reopens only the
  backend window; Metro keeps running, with its scrollback intact.
- **Signal before close.** `down` sends SIGTERM to the process group, waits
  `stop_timeout`, then SIGKILL, then closes the window. Closing first would leave a
  process holding the port with nothing on screen to explain it.
- **Ownership is proven, not assumed.** devpad records the pid, the process-group id
  and the process start time, and requires positive evidence before signalling
  anything. It will not kill a stranger that happens to reuse an old pid.
- **Windowed output is recorded.** A service running in a window also writes to
  `.devpad/logs/<service>.log`, so `devpad logs <svc>` works whichever mode you used —
  useful for an agent that needs to diagnose without reading the screen.
- **Crashes are reported immediately.** A service that dies during startup is
  reported with its exit code in well under a second, not after a 90-second timeout.

---

## Config

```yaml
version: 1
name: myapp

terminal:
  driver: auto                # auto | iterm | apple-terminal | tmux | command-file | direct
  hold: true                  # keep the window after the process exits
  title: "{project} · {service}"

services:
  infra:
    kind: docker
    dir: ./infra
    compose: docker-compose.yml
    services: [mysql, redis]
    ready: { tcp: 3306, timeout: 120 }
    terminal: false           # a database container does not need its own window

  api:
    kind: proc
    dir: ./server
    command: uvicorn app.main:app --reload --port 8080
    env: { PYTHONUNBUFFERED: "1" }
    port: 8080
    ready: { http: "http://127.0.0.1:8080/health", timeout: 90 }
    depends_on: [infra]

  app:
    kind: proc
    dir: ./mobile
    command: npx react-native start
    port: 8081
    depends_on: [api]
    ready: { tcp: 8081, timeout: 180 }
    terminal: { hold: false }
```

`kind`: `proc` (long-running local process), `docker` (compose group), `cmd`
(one-shot). `ready` accepts one of `http` / `tcp` / `port` / `cmd` plus `timeout`;
omit the probe for slow builds. `depends_on` gives start order and expands a targeted
restart to include prerequisites.

Full reference: [`references/devpad-reference.md`](references/devpad-reference.md).

---

## What the agent gets taught

Installing the skill (see [Install](#install)) changes three behaviours that an
agent otherwise gets wrong:

1. **Restart exactly what was named.** `devpad restart api` closes and reopens only
   the backend window; Metro keeps running with its scrollback.
2. **Never bounce the database as a side effect.** `depends_on` pulls a dependency in
   for the *start* half only, so `restart api` leaves postgres running.
3. **Read the log instead of claiming success.** Windowed output is captured to
   `.devpad/logs/<service>.log`, so `devpad logs <svc>` works even while the window
   is open — no screen-scraping, no guesswork.

### Hosts that read `AGENTS.md` instead of skills

Add a few lines to the project and any agent will use the same commands:

```markdown
## Running this project
Start:   devpad up --terminal
Restart: devpad restart api        # or app, web
Status:  devpad status
Logs:    devpad logs api -n 80
Never run `devpad down` or `devpad rebuild` without asking.
```

### A caveat worth knowing

Opening a window needs the agent host to be allowed to send Apple Events. Sandboxed
hosts cannot; `devpad doctor` reports it, and the skill tells the agent to fall back
to detached mode plus `devpad logs` rather than pretending a window appeared.

## Requirements

- macOS
- Node 18+
- Python 3.8+ (no `pip install` — standard library only)

devpad installs as a thin Node shim over a Python engine: everything is verified
standard-library code, with **no runtime dependencies**. Set `DEVPAD_PYTHON` to
choose a specific interpreter.

---

## Development

```bash
python3 tests/test_terminals.py     # driver contract, no windows opened
python3 tests/check_static.py       # undefined names, YAML round-trip, CLI surface
python3 tests/test_devpad.py        # end-to-end lifecycle
python3 poc/terminal_probe.py       # prove the open/close channels on YOUR machine
```

The probe is worth running first: it opens real windows, prints the exit code, and
shows whether your setup can drive iTerm and Terminal.

---

## License

MIT
