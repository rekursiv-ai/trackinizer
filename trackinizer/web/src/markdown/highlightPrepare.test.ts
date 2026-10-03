import { expect, test } from "vitest";
import { highlightLines, prepare } from "./highlight";

// Each file has the app's modules afresh (src/testSetup.ts), so the highlighter starts unprepared here.
test("the highlighter registers its languages a few a task, once however often asked, then colours tags, aliases and untagged code", async () => {
  expect(highlightLines("ls -la", "bash")).toBeUndefined();
  const prepared = prepare();
  expect(prepare()).toBe(prepared);
  // The first few languages register in the task that asked; the rest in tasks after it.
  expect(highlightLines("ls -la", "bash")).toBeDefined();
  expect(highlightLines("x = 1", "python")).toBeUndefined();
  await prepared;
  expect(highlightLines("x = 1", "py")?.flat().some((node) => node.type === "element")).toBe(true);
  expect(highlightLines("def read(path):\n    return path.read_text()", "")?.[0]?.some((node) => node.type === "element")).toBe(true);
});
