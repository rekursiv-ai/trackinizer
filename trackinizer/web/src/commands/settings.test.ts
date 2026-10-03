import { expect, test } from "vitest";
import { settingsCommands } from "./settings";

test("everyone can go to Your settings; only an admin is offered Admin", () => {
  const gone: string[] = [];
  const admin = settingsCommands("admin", (screen) => gone.push(screen));
  expect(admin.map(({ run, ...declared }) => declared)).toEqual([
    { id: "go.settings", title: "Go to Your settings", section: "Navigate" },
    { id: "go.admin", title: "Go to Admin", section: "Navigate" },
  ]);
  admin.forEach((command) => command.run());
  expect(gone).toEqual(["settings", "admin"]);
  for (const role of ["writer", "viewer"]) {
    expect(settingsCommands(role, () => {}).map((command) => command.id)).toEqual(["go.settings"]);
  }
});
