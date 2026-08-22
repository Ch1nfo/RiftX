import { createBashToolDefinition, type BashToolOptions } from "@mariozechner/pi-coding-agent";
import { DEFAULT_BASH_TIMEOUT_SECONDS, MAX_BASH_TIMEOUT_SECONDS, resolveBashTimeout } from "./bash-timeout-policy";

export function createTimedBashTool(cwd: string, options?: BashToolOptions) {
  const bash = createBashToolDefinition(cwd, options);
  const execute = bash.execute.bind(bash);
  bash.description += ` Commands time out after ${DEFAULT_BASH_TIMEOUT_SECONDS} seconds by default and cannot run longer than ${MAX_BASH_TIMEOUT_SECONDS} seconds.`;
  Object.assign(bash.parameters.properties.timeout, { description: `Timeout in seconds; defaults to ${DEFAULT_BASH_TIMEOUT_SECONDS} and is capped at ${MAX_BASH_TIMEOUT_SECONDS}` });
  bash.execute = (toolCallId, input, signal, onUpdate, context) => execute(toolCallId, { ...input, timeout: resolveBashTimeout(input.timeout) }, signal, onUpdate, context);
  return bash;
}
