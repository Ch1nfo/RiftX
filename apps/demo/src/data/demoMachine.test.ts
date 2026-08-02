import { describe, expect, it } from "vitest";

import { createInitialDemoState, demoReducer } from "./demoMachine";

describe("demoReducer", () => {
  it.each([
    {
      decision: "approved" as const,
      runStatus: "running",
      messageRole: "agent",
      eventType: "approval.approved",
      announcement: "Approval granted. Synthetic execution started.",
    },
    {
      decision: "rejected" as const,
      runStatus: "waiting_user",
      messageRole: "system",
      eventType: "approval.rejected",
      announcement: "Approval rejected. No external effect was produced.",
    },
  ])(
    "persists an $decision approval decision without mutating the previous state",
    ({ decision, runStatus, messageRole, eventType, announcement }) => {
      const initial = createInitialDemoState();

      const next = demoReducer(initial, { type: "approval", decision });

      expect(next).not.toBe(initial);
      expect(next.approvalStatus).toBe(decision);
      expect(next.runStatus).toBe(runStatus);
      expect(next.messages).toHaveLength(initial.messages.length + 1);
      expect(next.messages.at(-1)).toMatchObject({ role: messageRole });
      expect(next.timeline).toHaveLength(initial.timeline.length + 1);
      expect(next.timeline[0]).toMatchObject({ type: eventType });
      expect(next.announcement).toBe(announcement);

      expect(initial.approvalStatus).toBe("pending");
      expect(initial.runStatus).toBe("waiting_approval");
    },
  );

  it("pauses a running Run and resumes the same Run", () => {
    const running = demoReducer(createInitialDemoState(), {
      type: "approval",
      decision: "approved",
    });

    const paused = demoReducer(running, { type: "pause" });

    expect(paused.runStatus).toBe("paused");
    expect(paused.timeline[0]).toMatchObject({
      type: "run.paused",
      title: "Run paused",
    });
    expect(paused.announcement).toBe("Run paused. Every synthetic effect is confirmed.");

    const resumed = demoReducer(paused, { type: "pause" });

    expect(resumed.runStatus).toBe("running");
    expect(resumed.timeline[0]).toMatchObject({
      type: "run.resumed",
      title: "Run resumed",
    });
    expect(resumed.announcement).toBe("Run resumed.");
  });

  it.each(["waiting_approval", "waiting_user", "completed", "cancelled"] as const)(
    "ignores pause actions while the Run is %s",
    (runStatus) => {
      const state = { ...createInitialDemoState(), runStatus };

      const next = demoReducer(state, { type: "pause" });

      expect(next).toBe(state);
      expect(next.timeline).toBe(state.timeline);
      expect(next.announcement).toBe(state.announcement);
    },
  );

  it.each([
    ["approved", "approved"],
    ["approved", "rejected"],
    ["rejected", "rejected"],
    ["rejected", "approved"],
  ] as const)(
    "treats a %s approval followed by %s as an idempotent replay",
    (firstDecision, replayDecision) => {
      const decided = demoReducer(createInitialDemoState(), {
        type: "approval",
        decision: firstDecision,
      });

      const replayed = demoReducer(decided, {
        type: "approval",
        decision: replayDecision,
      });

      expect(replayed).toBe(decided);
      expect(replayed.approvalStatus).toBe(firstDecision);
      expect(replayed.messages).toBe(decided.messages);
      expect(replayed.timeline).toBe(decided.timeline);
    },
  );

  it("records affirmative stop proof and rejects an undecided approval", () => {
    const initial = createInitialDemoState();

    const stopped = demoReducer(initial, { type: "stop" });

    expect(stopped.runStatus).toBe("cancelled");
    expect(stopped.stopProof).toBe(true);
    expect(stopped.approvalStatus).toBe("rejected");
    expect(stopped.timeline[0]).toMatchObject({
      type: "run.cancelled",
      tone: "danger",
    });
    expect(stopped.announcement).toBe(
      "Emergency stop completed. All three effect owners confirmed stop.",
    );
  });

  it("preserves an approved decision when the Run is stopped", () => {
    const approved = demoReducer(createInitialDemoState(), {
      type: "approval",
      decision: "approved",
    });

    const stopped = demoReducer(approved, { type: "stop" });

    expect(stopped.runStatus).toBe("cancelled");
    expect(stopped.stopProof).toBe(true);
    expect(stopped.approvalStatus).toBe("approved");
  });

  it("reset returns a fresh, complete initial state", () => {
    const initial = createInitialDemoState();
    const approved = demoReducer(initial, { type: "approval", decision: "approved" });
    const paused = demoReducer(approved, { type: "pause" });
    const navigated = demoReducer(paused, { type: "navigate", view: "connectors" });

    const reset = demoReducer(navigated, { type: "reset" });

    expect(reset).toEqual(createInitialDemoState());
    expect(reset).not.toBe(initial);
    expect(reset.messages).not.toBe(initial.messages);
    expect(reset.timeline).not.toBe(initial.timeline);
  });

  it("rebuilds every localized seed when the locale changes", () => {
    const english = demoReducer(createInitialDemoState("en"), {
      type: "approval",
      decision: "approved",
    });

    const chinese = demoReducer(english, { type: "set-locale", locale: "zh-CN" });

    expect(chinese.locale).toBe("zh-CN");
    expect(chinese.view).toBe("overview");
    expect(chinese.approvalStatus).toBe("pending");
    expect(chinese.announcement).toBe("演示环境已就绪。");
    expect(chinese.messages[0].text).toContain("行动上下文已持久化");
    expect(chinese.messages.some((message) => message.text.includes("Approval was persisted"))).toBe(false);
    expect(chinese.timeline[0].title).toBe("Target HTTP 等待独立批准");
  });
});
