import assert from "node:assert/strict";
import test from "node:test";
import { buildPentestCompactionPrompt, isValidPentestCompactionSummary, PENTEST_COMPACTION_SYSTEM_PROMPT } from "./compaction-prompt";

const canonicalHeadings = [
  "Run state",
  "Current challenge",
  "Challenge blackboard",
  "Confirmed facts",
  "Attempt history and exclusion evidence",
  "Target credentials and session state",
  "Browser and network references",
  "Artifacts",
  "SubAgent ownership",
  "Exact next probe",
  "Score optimization notes"
] as const;

const aliasHeadings = [
  "Run status",
  "Current task",
  "Blackboard",
  "Verified facts",
  "Attempts and exclusions",
  "Credentials and session state",
  "Browser/network references",
  "Evidence artifacts",
  "Subagent assignments",
  "Next action",
  "Score notes"
] as const;

const legacyAttemptHeadings = [
  "Attempts and ruled-out paths",
  "Attempt and approach history",
  "Ruled-out assumptions"
] as const;

function section(heading: string, body = "Synthetic fixture evidence.") {
  return `## ${heading}\n${body}\n`;
}

function summary(headings: readonly string[] = canonicalHeadings) {
  return headings.map((heading) => section(heading)).join("\n");
}

function legacySummary(legacy: readonly string[], empty?: string) {
  return canonicalHeadings.flatMap((heading) => heading === canonicalHeadings[4]
    ? legacy.map((old) => section(old, old === empty ? " \t " : undefined))
    : [section(heading)]).join("\n");
}

test("system and generated compaction requests use exactly the eleven canonical headings", () => {
  const headings = (text: string) => [...text.matchAll(/^## .+$/gm)].map(([heading]) => heading.slice(3));
  assert.deepEqual(headings(PENTEST_COMPACTION_SYSTEM_PROMPT), canonicalHeadings);
  assert.deepEqual(headings(buildPentestCompactionPrompt({ conversation: "fixture" })), canonicalHeadings);
});

test("accepts eleven populated canonical sections", () => {
  assert.equal(isValidPentestCompactionSummary(summary()), true);
});

test("accepts explicit absence of recorded information as section content", () => {
  const input = canonicalHeadings.map((heading) => section(heading, "None recorded")).join("\n");
  assert.equal(isValidPentestCompactionSummary(input), true);
});

test("accepts the finite aliases for all eleven canonical sections", () => {
  assert.equal(isValidPentestCompactionSummary(summary(aliasHeadings)), true);
});

test("accepts heading numbers with periods or closing parentheses", () => {
  const headings = canonicalHeadings.map((heading, index) => `${index + 1}${index % 2 ? ")" : "."} ${heading}`);
  assert.equal(isValidPentestCompactionSummary(summary(headings)), true);
});

test("accepts case differences and whitespace around and within headings", () => {
  const headings = canonicalHeadings.map((heading) => ` \t${heading.toUpperCase().replaceAll(" ", " \t ")}  \t`);
  assert.equal(isValidPentestCompactionSummary(summary(headings)), true);
});

test("accepts bold heading text and a trailing colon", () => {
  assert.equal(isValidPentestCompactionSummary(summary(canonicalHeadings.map((heading) => `**${heading}**:`))), true);
});

test("accepts combined numbering, bold aliases, case differences and a trailing colon", () => {
  const headings = aliasHeadings.map((heading, index) => `${index + 1}) **${heading.toUpperCase()}**:`);
  assert.equal(isValidPentestCompactionSummary(summary(headings)), true);
});

for (const [index, heading] of canonicalHeadings.entries()) {
  test(`rejects a missing required section: ${heading}`, () => {
    assert.equal(isValidPentestCompactionSummary(summary(canonicalHeadings.filter((_, i) => i !== index))), false);
  });

  test(`rejects an empty required section: ${heading}`, () => {
    const input = canonicalHeadings.map((name, i) => section(name, i === index ? " \t\n\t " : undefined)).join("\n");
    assert.equal(isValidPentestCompactionSummary(input), false);
  });

  test(`a duplicate alias cannot replace the missing section: ${heading}`, () => {
    const headings = canonicalHeadings.map((name, i) => i === index ? aliasHeadings[(index + 1) % aliasHeadings.length] : name);
    assert.equal(isValidPentestCompactionSummary(summary(headings)), false);
  });
}

test("rejects headings that appear only inside a fenced code block", () => {
  for (const fence of ["```", "~~~"]) {
    assert.equal(isValidPentestCompactionSummary(`${fence}markdown\n${summary()}${fence}\n`), false);
  }
});

test("rejects numbered headings and bodies indented as code with four spaces", () => {
  const input = canonicalHeadings.map((heading, index) => `    ${index + 1}. ${heading}\n    Synthetic fixture evidence.\n`).join("\n");
  assert.equal(isValidPentestCompactionSummary(input), false);
});

test("rejects Markdown headings and bodies indented as code with a tab", () => {
  const input = canonicalHeadings.map((heading) => `\t## ${heading}\n\tSynthetic fixture evidence.\n`).join("\n");
  assert.equal(isValidPentestCompactionSummary(input), false);
});

test("a title mentioned within body text cannot supply a missing section", () => {
  const missing = canonicalHeadings[3];
  const input = summary(canonicalHeadings.filter((heading) => heading !== missing))
    + `\nSynthetic fixture text mentions ## ${missing} without adding a section.\n`;
  assert.equal(isValidPentestCompactionSummary(input), false);
});

test("a fenced section cannot supply an otherwise missing required section", () => {
  const missing = canonicalHeadings[3];
  const input = summary(canonicalHeadings.filter((heading) => heading !== missing))
    + `\n\`\`\`markdown\n${section(missing)}\`\`\`\n`;
  assert.equal(isValidPentestCompactionSummary(input), false);
});

test("accepts the legacy thirteen-section layout when all three attempt sections have content", () => {
  assert.equal(isValidPentestCompactionSummary(legacySummary(legacyAttemptHeadings)), true);
});

for (const heading of legacyAttemptHeadings) {
  test(`rejects an incomplete legacy layout without: ${heading}`, () => {
    assert.equal(isValidPentestCompactionSummary(legacySummary(legacyAttemptHeadings.filter((name) => name !== heading))), false);
  });

  test(`rejects an empty legacy attempt section: ${heading}`, () => {
    assert.equal(isValidPentestCompactionSummary(legacySummary(legacyAttemptHeadings, heading)), false);
  });

  test(`one legacy attempt section cannot replace all three: ${heading}`, () => {
    assert.equal(isValidPentestCompactionSummary(legacySummary([heading])), false);
  });
}
