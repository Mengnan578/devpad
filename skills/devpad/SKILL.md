---
name: devpad
description: "Open a visible terminal window per service and start the dev stack inside it — React Native / Metro, Python backends, and docker compose — so the logs stay on the user's screen. Use when the user asks to restart the frontend or backend, wants to watch Metro or uvicorn output, hits a port conflict, asks why a service will not start, or wants a one-command rebuild."
whenToUse: "Local development where restarting services is frequent and the output must be visible in real terminal windows, while still being readable by an agent."
---

# devpad — put each service in its own window

The user's stated pain is not "start my project". It is:

> every day I restart the frontend and backend, the commands are long, and when an
> AI starts them the terminal never appears, so I cannot see the logs I need.

So the deliverable is not just "start the service". It is **open a window, run the
service there, and make sure the log survives the process**. Judge every action
against that.

## How to run it

Prefer the installed command when it exists:

```bash
command -v devpad && devpad status
```

If `devpad` is not on `PATH`, run the engine that ships inside this package and
substitute it for `devpad` in every command below:

```bash
# Claude Code plugin install: the host sets CLAUDE_PLUGIN_ROOT
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/devpad.py" status

# Any other install: this SKILL.md is at <package>/skills/devpad/SKILL.md,
# so the engine is two directories up.
python3 "$(cd "$(dirname "$0")/../.." && pwd)/scripts/devpad.py" status
```

Determine which one applies once, at the start, then use that form consistently.
Do not assume `devpad` exists — a plugin install deliberately does not require a
global command.

## The one command that matters

```bash
devpad up --terminal        # one visible window per proc service, logs on screen
```

Everything else supports that. If the user asks to restart something and wants to
see it, the answer is `devpad restart <service> --terminal`, not a background job
with redirected output.

## Commands

| Command | What it does |
|---|---|
| `devpad up --terminal` | Start the stack in dependency order; each proc service gets its own window. |
| `devpad up` | Same, but detached with logs in `.devpad/logs/` (CI, or when no window is wanted). |
| `devpad restart api --terminal` | Restart only the backend and reopen only its window. The other windows stay untouched. |
| `devpad restart api` | Restart without a window. |
| `devpad down` | Stop processes, then close the windows it opened. |
| `devpad status` | Table: state, pid, uptime, port, health, **window** (driver + tty), kind. |
| `devpad logs -f api` | Follow a service's output; works for windowed services too. |
| `devpad attach api` | Run one service in *this* terminal with a real TTY. |
| `devpad doctor` | Tooling, paths, ports, docker, **and the terminal-driver matrix**. |
| `devpad detect` / `init --detect` | Derive a config from the project (see below). |
| `devpad ports` / `kill 8081` | Who holds a port, and freeing it. |
| `devpad rebuild api --dry-run` | stop → clean → install → build → start, printed first. |

Exit codes: `0` success, `1` a required service failed or a decision is pending, `2` invalid config.

## Platform notes

macOS uses AppleScript drivers (`iterm`, `apple-terminal`) and a generated shell
runner. Windows uses `windows-terminal` / `cmd-window`, which run the engine's own
`run` subcommand inside the tab — so pid tracking and log capture work there too.

On Windows, `devpad doctor` is the only trustworthy statement about window support:
the logic is CI-tested, but window opening is **not verified on a real Windows
machine**. Say so rather than assuming it works.

## Verify before promising

Windows depend on the host being allowed to talk to the terminal app. Before saying
"a window will open", check the matrix:

```bash
devpad doctor
```

It prints every driver and, at the end, which one `auto` would pick. Two rules:

1. **Never claim a window opened without checking.** Confirm with `devpad status`
   (the WINDOW column) or by asking the user what appeared.
2. If the host cannot send Apple Events, `auto` falls back to `command-file`, which
   needs no such permission but **cannot close its own window**. Say that out loud
   instead of hiding it.

## Showing output — pick the right mechanism

Windowed and detached services both record to `.devpad/logs/<service>.log`. The window
is for the human; the file is what `devpad logs` reads, so an agent can diagnose a
service running in a window without screen-scraping the terminal.

| Need | Use |
|---|---|
| Logs the user must read, scroll, copy | `devpad up --terminal` / `restart <svc> --terminal` |
| Read what a windowed service printed | `devpad logs api -n 80 --no-prefix` — window output is captured too |
| Live stream into the transcript | `devpad logs -f api` (run as a background job; it never exits) — works for windowed services as well |
| Interactive/hotkeys (simulator, REPL) | `devpad attach api` |
| A service that must not own a window | `terminal: false` on that service |

## Before anything else: is there a config?

```bash
devpad config          # resolved root + services
devpad init --detect   # derive one from the project
```

`--detect` reads `package.json`, the Python manifest and the compose file, and is
explicit about confidence: unmarked (read from a file), `LIKELY` (inferred),
`GUESS` (must be confirmed). Anything undecidable is listed under `NEEDS A DECISION`
and the command exits `1`. In that case **ask the user those specific questions**;
do not resolve them with a plausible default. It also upgrades readiness to HTTP
when the source actually defines a `/health`-style route.

Docker services default to `terminal: false` — a database container does not need a
window; its logs belong in `devpad logs`.

## Config essentials

```yaml
version: 1
name: myapp

terminal:
  driver: auto      # auto | iterm | apple-terminal | tmux | command-file | direct
  hold: true        # keep the window open after the process exits
  title: "{project} · {service}"

services:
  infra:
    kind: docker
    dir: ./infra
    compose: docker-compose.yml
    services: [mysql, redis]
    ready: { tcp: 3306, timeout: 120 }
    terminal: false
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

`hold: true` is the default because a crashed service's last lines are exactly what
the user needs. `hold: false` suits processes that exit cleanly on purpose.

## Rules that keep this trustworthy

- **Prefer one service over the whole stack.** `restart api --terminal` is the
  normal action; `up --terminal` on a running stack is for a cold start.
- **Never `down` or `rebuild` unasked.** `down` closes windows and removes
  containers; that can discard in-flight work.
- **Signal before closing a window.** Closing first leaves a process holding the
  port with nothing on screen to explain it. devpad already does this — do not
  work around it by closing windows manually.
- **Report failures with the real reason**, including the driver name and the
  `-10004` / permission hint, rather than "it did not work".
- **Do not invent start commands.** Use `init --detect`, then confirm what it marked.

## When something will not start

1. `devpad doctor` — missing binary, wrong dir, busy port, docker down, driver unavailable.
2. `devpad status` — is it down, or up on a port you did not expect?
3. `devpad logs <svc> -n 120 --no-prefix` — the real error is usually here.
4. `devpad ports` then `devpad kill <port>` — port conflicts are the most common cause.
5. `devpad rebuild <svc> --dry-run` before letting it delete anything.

Full field reference, driver matrix and troubleshooting: `references/devpad-reference.md`
in the package root — the directory two levels above this skill file, or
`${CLAUDE_PLUGIN_ROOT}` when installed as a plugin.
