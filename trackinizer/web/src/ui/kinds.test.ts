import { expect, test } from "vitest";
import { kindLook } from "./kinds";

const KINDS = ["Issue", "Belief", "Experiment", "Paper", "Artifact", "CodeChange", "WebSearch", "WebResult", "AgentSession"];

test("each kind has an icon of its own, and none is the search button's magnifier", () => {
  const icons = KINDS.map((kind) => kindLook(kind).icon);
  expect(new Set(icons).size).toBe(KINDS.length);
  expect(icons).not.toContain("search");
});
