import type { BenchmarkLedger, ChallengeOwner, ChallengeState } from "./ledger";

type Warning = Pick<ChallengeState, "uniqueCode" | "currentAttemptStartedAt"> & { packet: string };
type Message = { content: string | readonly { type: string; text?: string }[] };

// Preparing or rebuilding context must not consume a notice. Acknowledge only
// a packet present in the sampled request that produced a successful reply.
export class BenchmarkWarningDelivery {
  private prepared?: Warning;
  private sampled?: Warning;

  constructor(private readonly ledger: Pick<BenchmarkLedger, "acknowledgeAttemptWarning">,
    private readonly owner: Exclude<ChallengeOwner, null>) {}

  prepare(warning?: Pick<ChallengeState, "uniqueCode" | "currentAttemptStartedAt">, packet = "") {
    this.prepared = warning && packet ? {
      uniqueCode: warning.uniqueCode, currentAttemptStartedAt: warning.currentAttemptStartedAt, packet
    } : undefined;
  }

  sample(messages: readonly Message[]) {
    const warning = this.prepared;
    this.sampled = warning && messages.some(({ content }) => {
      const text = typeof content === "string" ? content : content.filter((part) => part.type === "text").map((part) => part.text ?? "").join("\n");
      return text.includes(warning.packet);
    }) ? warning : undefined;
  }

  async complete(message: { role: string; stopReason?: string }) {
    if (message.role !== "assistant") return;
    const warning = this.sampled;
    this.sampled = undefined;
    if (warning && message.stopReason && !["error", "aborted"].includes(message.stopReason)) {
      await this.ledger.acknowledgeAttemptWarning(this.owner, warning.uniqueCode, warning.currentAttemptStartedAt);
    }
  }
}
