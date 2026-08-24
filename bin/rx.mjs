#!/usr/bin/env node

import { spawn } from "node:child_process";
import { access } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const [command, ...args] = process.argv.slice(2);

if (!command || command === "help" || command === "--help" || command === "-h") {
  console.log("Usage: rx webui [--port <port>] [--hostname <hostname>]");
  process.exit(0);
}

if (command !== "webui") {
  console.error(`Unknown command: ${command}`);
  console.error("Usage: rx webui [--port <port>] [--hostname <hostname>]");
  process.exit(1);
}

try {
  await access(join(appRoot, ".next", "BUILD_ID"));
} catch {
  console.error("RiftX production build is missing. Reinstall RiftX and try again.");
  process.exit(1);
}

const child = spawn(
  process.execPath,
  [join(appRoot, "node_modules", "next", "dist", "bin", "next"), "start", ...args],
  {
    cwd: appRoot,
    stdio: "inherit",
    env: { ...process.env, RIFTX_LAUNCH_CWD: process.cwd() }
  }
);

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => child.kill(signal));
}

child.once("error", (error) => {
  console.error(`Unable to start RiftX: ${error.message}`);
  process.exitCode = 1;
});

child.once("exit", (code) => {
  process.exitCode = code ?? 1;
});
