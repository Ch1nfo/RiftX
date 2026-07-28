import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const desktopDirectory = resolve(scriptDirectory, "..");
const repositoryRoot = resolve(desktopDirectory, "../..");
const codexDirectory = join(repositoryRoot, "codex-rs");
const binariesDirectory = join(desktopDirectory, "src-tauri", "binaries");
const profile =
  process.env.RIFTX_SIDECAR_PROFILE ??
  (process.env.TAURI_ENV_DEBUG === "true" ? "debug" : "release");

if (profile !== "debug" && profile !== "release") {
  fail(`unsupported sidecar profile ${JSON.stringify(profile)}`);
}

const hostTriple = output("rustc", ["--print", "host-tuple"]);
const targetTriple = process.env.RIFTX_SIDECAR_TARGET ?? hostTriple;
const extension = targetTriple.includes("windows") ? ".exe" : "";
const cargoArguments = [
  "build",
  "--manifest-path",
  join(codexDirectory, "Cargo.toml"),
  "-p",
  "codex-riftx-gateway",
  "--bin",
  "riftxd",
];

if (profile === "release") {
  cargoArguments.push("--release");
}
if (targetTriple !== hostTriple) {
  cargoArguments.push("--target", targetTriple);
}

run("cargo", cargoArguments);

const targetDirectory = process.env.CARGO_TARGET_DIR
  ? resolve(repositoryRoot, process.env.CARGO_TARGET_DIR)
  : join(codexDirectory, "target");
const sourceDirectory =
  targetTriple === hostTriple
    ? join(targetDirectory, profile)
    : join(targetDirectory, targetTriple, profile);
const source = join(sourceDirectory, `riftxd${extension}`);
const destination = join(
  binariesDirectory,
  `riftxd-${targetTriple}${extension}`,
);

if (!existsSync(source)) {
  fail(`built sidecar is missing at ${source}`);
}
mkdirSync(binariesDirectory, { recursive: true });
copyFileSync(source, destination);
if (process.platform !== "win32") {
  chmodSync(destination, 0o755);
}
console.log(`Prepared ${destination}`);

function output(command, arguments_) {
  const result = spawnSync(command, arguments_, {
    cwd: repositoryRoot,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    fail(result.stderr?.trim() || `${command} exited with ${result.status}`);
  }
  return result.stdout.trim();
}

function run(command, arguments_) {
  const result = spawnSync(command, arguments_, {
    cwd: repositoryRoot,
    stdio: "inherit",
  });
  if (result.status !== 0) {
    fail(`${command} exited with ${result.status}`);
  }
}

function fail(message) {
  console.error(`prepare-sidecar: ${message}`);
  process.exit(1);
}
