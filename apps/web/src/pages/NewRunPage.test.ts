import { describe, expect, it } from "vitest";

import { parseEntryPoints } from "./NewRunPage";

describe("new run entry point parsing", () => {
  it("converts one KIND=VALUE entry per line", () => {
    expect(
      parseEntryPoints("url=https://example.test\nip=10.10.10.20\n"),
    ).toEqual([
      { kind: "url", value: "https://example.test" },
      { kind: "ip", value: "10.10.10.20" },
    ]);
  });

  it("rejects unsupported entry point kinds", () => {
    expect(() => parseEntryPoints("host=example.test")).toThrow(
      "Unsupported entry point kind",
    );
  });
});
