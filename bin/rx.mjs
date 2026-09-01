#!/usr/bin/env node

import { spawn } from "node:child_process";
import { access, readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const { version: appVersion } = JSON.parse(await readFile(join(appRoot, "package.json"), "utf8"));
const [command, ...args] = process.argv.slice(2);

if (command === "version" || command === "--version" || command === "-v") {
  console.log(`RiftX ${appVersion}`);
  process.exit(0);
}

if (!command || command === "help" || command === "--help" || command === "-h") {
  console.log("Usage: rx webui [--port <port>] [--hostname <hostname>]");
  console.log("       rx --version");
  console.log("       Binds 127.0.0.1 by default; set RIFTX_HOST or --hostname to expose.");
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

// Default to loopback: the API can drive local tooling (bash, MCP servers),
// so it must not be reachable from the network unless the operator opts in
// with --hostname/-H or RIFTX_HOST.
const hostnameArgIndex = args.findIndex((arg) => arg === "-H" || arg === "--hostname");
const launchArgs = hostnameArgIndex === -1 ? [...args, "-H", process.env.RIFTX_HOST ?? "127.0.0.1"] : args;

const child = spawn(
  process.execPath,
  [join(appRoot, "node_modules", "next", "dist", "bin", "next"), "start", ...launchArgs],
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
