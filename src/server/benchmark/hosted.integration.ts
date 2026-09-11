import assert from "node:assert/strict";
import test from "node:test";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { MockBenchmarkApi } from "../../../tests/e2e/mock-benchmark-api";
import { benchmarkWorkspaceRoot, challengeDirectory } from "./workspace";

// This fixture drives the real SDK and runner; it never contacts a real model or target.
// Set RIFTX_TEST_IMAGE to run the same checks through the image's default entrypoint.
for (const mode of ["complete", "signal", "delegate", "abstain", "parallel", "recover"]) test(`headless ${mode}: lifecycle and active skill delivery`, { timeout: 120_000 }, async () => {
  const stopEarly = mode === "signal";
  const delegate = mode === "delegate" || mode === "parallel";
  const parallel = mode === "parallel";
  const directory = await mkdtemp(join(tmpdir(), "riftx-hosted-test-"));
  // Synthetic fixtures only. The bind mount masks all operator-provided skills
  // when testing an image, so tests never inspect recommended-skills contents.
  const skillsDirectory = join(directory, ".riftx", "skills");
  const fixtureDirectory = join(skillsDirectory, "benchmark-fixture");
  const skillMarker = "SYNTHETIC_BENCHMARK_SKILL_MARKER";
  await mkdir(fixtureDirectory, { recursive: true });
  const description = "Synthetic widget inspection.";
  await writeFile(join(fixtureDirectory, "SKILL.md"), `---\nname: benchmark-fixture\ndescription: ${description}\n---\n${skillMarker}\n`);
  const platform = new MockBenchmarkApi();
  platform.seedChallenges(mode === "abstain" || parallel ? 2 : 1);
  const fixtures = (platform as unknown as { challenges: Map<string, { description: string }> }).challenges;
  fixtures.get("ch-001")!.description = "Inspect a synthetic widget.";
  if (mode === "abstain") fixtures.get("ch-002")!.description = "Analyze the provided ELF binary.";
  await platform.start();
  let requests = 0;
  let mainRequests = 0;
  let childRequests = 0;
  const bodies: Array<{ messages: unknown[]; tools: Array<{ function: { name: string } }>; max_tokens?: number; max_completion_tokens?: number }> = [];
  let releaseChild!: () => void;
  const mainResumed = new Promise<void>((resolve) => { releaseChild = resolve; });
  let resumedBeforeChild = false;
  let childReleasedBeforeMain = false;
  const steps = parallel ? ["idle", "sync", "assign", "idle", "acquire2", "bash2", "submit2a", "submit2b", "idle"] : stopEarly ? ["sync", "acquire", "hang"] : delegate ? ["idle", "sync", "assign", "idle"]
    : ["idle", "sync", "acquire", "bash", ...(mode === "recover" ? ["model-error"] : []), "submit", ...(mode === "abstain" ? ["acquire2", "bash2", "submit2a", "submit2b"] : []), "idle"];
  const llm = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw) as typeof bodies[number];
    const isAgent = Boolean(body.tools?.length);
    if (isAgent) bodies.push(body);
    const index = requests++;
    const isChild = isAgent && !body.tools.some((tool) => tool.function.name === "assign_benchmark_challenge");
    const step = !isAgent ? "idle" : isChild ? ["bash", "submit", "idle"][childRequests++] ?? "idle" : steps[mainRequests++] ?? "idle";
    if (parallel && step === "acquire2") { resumedBeforeChild = true; releaseChild(); }
    if (parallel && isChild && step === "bash") {
      // The old runner waited for this child before resuming the main worker.
      let timer: ReturnType<typeof setTimeout> | undefined;
      await Promise.race([mainResumed, new Promise<void>((resolve) => { timer = setTimeout(resolve, 10_000); })]);
      clearTimeout(timer);
      childReleasedBeforeMain = !resumedBeforeChild;
    }
    if (step === "hang") return;
    if (step === "model-error") {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: { message: "ECONNRESET synthetic provider failure", type: "fixture" } }));
      return;
    }
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    const code = step.includes("2") ? "ch-002" : "ch-001";
    const action = step.replace(/2[ab]?$/, "");
    const tool = action === "bash" ? { name: "bash", arguments: JSON.stringify({ command: `test ! -e same-name.txt && printf '${code}' > same-name.txt && printf runner-permission-ok` }) }
      : step === "assign" ? { name: "assign_benchmark_challenge", arguments: JSON.stringify({ uniqueCode: "ch-001" }) }
      : { name: "benchmark_control", arguments: JSON.stringify({ action, ...(action === "acquire" || action === "submit" ? { uniqueCode: code } : {}), ...(action === "submit" ? { flag: `flag{mock_${code}_${step === "submit2b" ? 1 : 0}}` } : {}) }) };
    const delta = step === "idle" ? { role: "assistant", content: isChild ? "FINDINGS: synthetic child final observation\nUNCERTAINTIES: synthetic evidence limit" : "This assistant turn is finished." }
      : { role: "assistant", tool_calls: [{ index: 0, id: `call_${index}`, type: "function", function: tool }] };
    const chunk = (delta: unknown, finish: string | null) => ({ id: `reply_${index}`, object: "chat.completion.chunk", created: 1, model: "fixture", choices: [{ index: 0, delta, finish_reason: finish }] });
    res.write(`data: ${JSON.stringify(chunk(delta, null))}\n\n`);
    res.write(`data: ${JSON.stringify(chunk({}, step === "idle" ? "stop" : "tool_calls"))}\n\n`);
    res.end("data: [DONE]\n\n");
  });
  await new Promise<void>((done) => llm.listen(0, "0.0.0.0", done));
  const llmPort = (llm.address() as { port: number }).port;
  const image = process.env.RIFTX_TEST_IMAGE;
  const host = image ? "host.docker.internal" : "127.0.0.1";
  const runtimeEnv = {
    NODE_ENV: "test" as const,
    BENCHMARK_BASE_URL: `http://${host}:${platform.port}`, BENCHMARK_TOKEN: "fixture-platform-secret",
    BENCHMARK_VPN_URL: `http://${host}:${platform.port}`,
    RIFTX_HOSTED: "0", RIFTX_LLM_BASE_URL: `http://${host}:${llmPort}/v1`,
    RIFTX_LLM_MODEL: "fixture", RIFTX_LLM_API_KEY: "fixture-model-secret", RIFTX_LLM_MAX_TOKENS: "40000"
  };
  const containerName = `riftx-test-${process.pid}-${mode}`;
  const child = image
    ? spawn("docker", ["run", "--name", containerName, "--platform", "linux/amd64", "--mount", `type=bind,src=${skillsDirectory},dst=/root/.riftx/skills,readonly`, ...Object.entries(runtimeEnv).flatMap(([key, value]) => ["-e", `${key}=${value}`]), image], { stdio: ["ignore", "pipe", "pipe"] })
    : spawn(process.execPath, ["--import", resolve("node_modules/tsx/dist/loader.mjs"), resolve("src/server/benchmark/entry.ts")], {
      cwd: directory, env: { PATH: process.env.PATH, HOME: directory, TSX_TSCONFIG_PATH: resolve("tsconfig.json"), ...runtimeEnv }, stdio: ["ignore", "pipe", "pipe"]
    });
  let output = "";
  child.stdout.on("data", (chunk) => { output += String(chunk); });
  child.stderr.on("data", (chunk) => { output += String(chunk); });
  const exited = new Promise<number | null>((done, reject) => { child.on("exit", done); child.on("error", reject); });
  const docker = async (args: string[]) => {
    const cmd = spawn("docker", args, { stdio: "ignore" });
    return new Promise<number | null>((done, reject) => { cmd.on("exit", done); cmd.on("error", reject); });
  };
  const timeout = setTimeout(() => { child.kill("SIGKILL"); }, 100_000);
  try {
    if (stopEarly) {
      const limit = Date.now() + 60_000;
      while (requests < 3 && child.exitCode === null && Date.now() < limit) await delay(100);
      assert.equal(platform.getActiveContainers(), 1, output);
      if (image) assert.equal(await docker(["kill", "--signal", "TERM", containerName]), 0);
      else child.kill("SIGTERM");
    }
    const code = await exited;
    assert.equal(code, stopEarly ? 143 : 0, output);
    assert.match(output, /"approvalMode":"full"/);
    assert.match(output, /"event":"cleanup_complete"/);
    if (mode === "recover") assert.match(output, /"event":"model_recovery"/);
    assert.equal(platform.getActiveContainers(), 0, output);
    if (stopEarly) assert.deepEqual(platform.getCloseCalls(), ["ch-001"]);
    else {
      assert.equal(platform.getSolvedCount(), mode === "abstain" || parallel ? 2 : 1, output);
      assert.equal(platform.getSubmittedFlags().length, mode === "abstain" || parallel ? 3 : 1);
      assert.ok(requests >= 6, "must continue after the initial idle turn");
      if (delegate) {
        assert.ok(childRequests >= 2, "delegated agent used its own skill-bearing requests");
        if (!image) {
          const session = output.split("\n").filter((line) => line.startsWith("{")).map((line) => JSON.parse(line)).find((event) => event.event === "session_started");
          const saved = await readFile(join(directory, ".riftx", "benchmark", session.sessionId, "state.json"), "utf8");
          assert.match(saved, /synthetic child final observation/);
        }
      }
      if (parallel) assert.ok(resumedBeforeChild && !childReleasedBeforeMain, "main must resume its own work while the child is still active");
      if (!delegate) assert.match(output, /"tool":"bash"/);
    }
    assert.ok(bodies.every((body) => (body.max_tokens ?? body.max_completion_tokens) === 40000));
    for (const body of bodies) {
      const context = JSON.stringify(body.messages);
      assert.ok(context.includes("benchmark-fixture"), "SDK skill catalog is visible");
      assert.equal(context.split(skillMarker).length - 1, context.includes("## My challenge: ch-001") ? 1 : 0,
        "active skill appears once while selected and disappears after abstention");
      const names = body.tools.map((tool) => tool.function.name);
      assert.ok(names.includes("crawl"));
      assert.ok(!names.some((name) => /search|cve|jina/.test(name)));
    }
    if (!image && !stopEarly) {
      const root = benchmarkWorkspaceRoot(directory, runtimeEnv.BENCHMARK_BASE_URL);
      for (const code of mode === "abstain" ? ["ch-001", "ch-002"] : ["ch-001"]) {
        assert.equal(await readFile(join(challengeDirectory(root, code), "work", "same-name.txt"), "utf8"), code);
      }
    }
    const configFile = join(directory, "config.json");
    if (image) assert.equal(await docker(["cp", `${containerName}:/root/.riftx/config.json`, configFile]), 0);
    const configText = await readFile(image ? configFile : join(directory, ".riftx/config.json"), "utf8");
    for (const secret of [runtimeEnv.BENCHMARK_TOKEN, runtimeEnv.RIFTX_LLM_API_KEY]) {
      assert.ok(!configText.includes(secret), "credentials must not persist in config");
      assert.ok(!output.includes(secret), "credentials must not appear in logs");
    }
  } finally {
    clearTimeout(timeout);
    child.kill("SIGKILL");
    if (image) await docker(["rm", "-f", containerName]);
    llm.closeAllConnections();
    llm.close();
    platform.close();
    await rm(directory, { recursive: true, force: true });
  }
});
