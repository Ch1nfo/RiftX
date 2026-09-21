import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "node:net";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const dir = await mkdtemp(join(tmpdir(), "riftx-package-"));
const npm = process.platform === "win32" ? "npm.cmd" : "npm";
function run(args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(npm, args, { cwd, shell: process.platform === "win32", stdio: ["ignore", "pipe", "inherit"] });
    let text = ""; child.stdout.on("data", (data) => { text += data; });
    child.on("error", reject); child.on("exit", (code) => code === 0 ? resolve(text) : reject(new Error(`npm ${args[0]} exited ${code}`)));
  });
}
let server;
try {
  const packedOutput = await run(["pack", "--ignore-scripts", "--json", "--pack-destination", dir], process.cwd());
  // Some npm releases still run prepare for pack and prefix JSON with its stdout.
  const [packed] = JSON.parse(packedOutput.slice(packedOutput.lastIndexOf("\n[") + 1));
  const project = join(dir, "install"); await mkdir(project);
  await writeFile(join(project, "package.json"), JSON.stringify({ private: true, name: "riftx-install-smoke", version: "1.0.0" }));
  await run(["install", "--ignore-scripts", "--no-audit", "--no-fund", join(dir, packed.filename)], project);
  await run(["rebuild", "better-sqlite3"], project);
  const require = createRequire(join(project, "package.json"));
  const Database = require("better-sqlite3");
  const db = new Database(join(project, "smoke.sqlite")); db.pragma("journal_mode = WAL");
  db.exec("CREATE TABLE smoke (id INTEGER PRIMARY KEY); INSERT INTO smoke VALUES (1)");
  if (db.prepare("SELECT count(*) AS n FROM smoke").get().n !== 1) throw new Error("Native SQLite smoke failed"); db.close();
  const pkg = join(project, "node_modules", "riftx");
  await readFile(join(pkg, ".next", "BUILD_ID"));
  const probe = createServer(); await new Promise((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const port = probe.address().port; await new Promise((resolve) => probe.close(resolve));
  server = spawn(process.execPath, [require.resolve("next/dist/bin/next"), "start", pkg, "-H", "127.0.0.1", "-p", String(port)], { cwd: project, stdio: ["ignore", "ignore", "inherit"] });
  server.on("error", (error) => { console.error(error.message); });
  let ready = false;
  for (let i = 0; i < 120; i++) {
    if (server.exitCode !== null) throw new Error(`Packaged server exited ${server.exitCode}`);
    try { const response = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(1000) }); if (response.ok) { ready = true; break; } } catch { /* bounded readiness wait */ }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (!ready) throw new Error("Packaged server did not start");
  console.log(`Package install, SQLite WAL and Next startup passed on ${process.platform} / ${process.version}`);
} finally {
  if (server?.exitCode === null) { server.kill(); await new Promise((resolve) => server.once("exit", resolve)); }
  await rm(dir, { recursive: true, force: true });
}
