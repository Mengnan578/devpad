#!/usr/bin/env node
/**
 * devpad — npm entry point.
 *
 * The engine is Python (scripts/devpad.py): process-group ownership, zombie
 * reaping, port guarding and the terminal drivers are all verified there, and
 * rewriting them would mean re-earning that trust. This file only locates a
 * Python 3 interpreter and hands over, so `npx devpad` works with no install
 * step and no runtime dependencies.
 *
 * Design notes:
 *  - stdio is inherited, so a service started in the foreground keeps its TTY,
 *    its colours, and Ctrl-C.
 *  - the exit code is forwarded unchanged; scripts and CI depend on it.
 *  - every failure mode here is a clear message, never a silent no-op.
 */

import { spawn } from "node:child_process";
import { existsSync, accessSync, constants } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";

const HERE = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = resolve(HERE, "..");
const ENGINE = join(PACKAGE_ROOT, "scripts", "devpad.py");

const DIM = "\u001b[2m";
const RED = "\u001b[31m";
const YELLOW = "\u001b[33m";
const OFF = "\u001b[0m";

function fail(message, hint) {
  console.error(`${RED}devpad:${OFF} ${message}`);
  if (hint) console.error(`${DIM}${hint}${OFF}`);
  process.exit(1);
}

function isExecutable(path) {
  try {
    accessSync(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Locate a Python 3 interpreter.
 *
 * An explicit override wins, then PATH, then the system locations. macOS no
 * longer ships a `/usr/bin/python`, but `python3` is present once the command
 * line tools are installed — which every developer machine with a compiler
 * already has.
 */
function findPython() {
  const override = process.env.DEVPAD_PYTHON;
  if (override) {
    if (isExecutable(override)) return override;
    fail(
      `DEVPAD_PYTHON points at ${override}, which is not executable.`,
      "Unset it to fall back to python3 from PATH.",
    );
  }
  const candidates = ["python3", "python"];
  for (const name of candidates) {
    const found = which(name);
    if (found) return found;
  }
  for (const absolute of ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]) {
    if (isExecutable(absolute)) return absolute;
  }
  fail(
    "no Python 3 interpreter found.",
    "Install the Xcode command line tools (`xcode-select --install`) or set DEVPAD_PYTHON.",
  );
}

function which(name) {
  const path = process.env.PATH || "";
  for (const dir of path.split(":")) {
    if (!dir) continue;
    const candidate = join(dir, name);
    if (isExecutable(candidate)) return candidate;
  }
  return null;
}

function main() {
  if (!existsSync(ENGINE)) {
    fail(
      `the engine is missing: ${ENGINE}`,
      "This usually means a partial install. Reinstall: npm i -g devpad",
    );
  }

  const python = findPython();

  // Interpreter version guard: the engine uses f-strings and dataclasses-free
  // syntax, but 3.8 is the honest floor and worth stating clearly.
  const child = spawn(python, [ENGINE, ...process.argv.slice(2)], {
    stdio: "inherit",
    env: process.env,
  });

  child.on("error", (error) => {
    fail(`cannot run ${python}: ${error.message}`, "Check that `python3 --version` works.");
  });

  // Forward the exit code exactly. A wrapper that swallowed it would break every
  // `if devpad up; then ...` in a script or CI job.
  child.on("exit", (code, signal) => {
    if (signal) {
      // Re-raise so the shell sees the same signal termination it would have seen
      // from the engine directly.
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });

  // Ctrl-C reaches the child through the shared terminal; don't double-signal.
  process.on("SIGINT", () => {});
  process.on("SIGTERM", () => {});
}

main();

// Silence an unused-import lint in editors that do not read the comment above.
void YELLOW;
