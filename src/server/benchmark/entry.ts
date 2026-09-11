import { format } from "node:util";
import { runBenchmark, redactRuntimeSecrets } from "./runner";

// SDK diagnostics also pass through redaction; never print injected credentials.
for (const method of ["log", "warn", "error"] as const) {
  const original = console[method].bind(console);
  console[method] = (...args: unknown[]) => original(redactRuntimeSecrets(format(...args)));
}
runBenchmark().then((code) => process.exit(code)).catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
