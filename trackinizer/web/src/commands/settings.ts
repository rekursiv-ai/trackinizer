import type { Command } from "./registry";

/**
 * The palette's way to Your settings, and to Admin for an admin only: a
 * non-admin is never offered a screen that would refuse them.
 */
export function settingsCommands(role: string, go: (screen: "settings" | "admin") => void): Command[] {
  const commands: Command[] = [{ id: "go.settings", title: "Go to Your settings", section: "Navigate", run: () => go("settings") }];
  if (role === "admin") commands.push({ id: "go.admin", title: "Go to Admin", section: "Navigate", run: () => go("admin") });
  return commands;
}
