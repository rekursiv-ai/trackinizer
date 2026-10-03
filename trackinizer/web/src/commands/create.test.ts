import { expect, test } from "vitest";
import { createCommands } from "./create";

test("each kind gets a New command in the palette's Create section, and C opens the one on screen", () => {
  const opened: string[] = [];
  const commands = createCommands(["Issue", "CodeChange"], "CodeChange", (kind) => opened.push(kind));
  expect(commands.map(({ run, ...declared }) => declared)).toEqual([
    { id: "create.Issue", title: "New issue", section: "Create" },
    { id: "create.CodeChange", title: "New code change", section: "Create", keys: ["c"] },
  ]);
  commands.forEach((command) => command.run());
  expect(opened).toEqual(["Issue", "CodeChange"]);
});
