// Test helpers for the debug module; only tests import this file.
import { onTestFinished } from "vitest";
import { type LogEntry, recentEvents } from "./log";

/** Give `navigator` a clipboard for this test; returns what is written to it. */
export function stubClipboard(): string[] {
  const copied: string[] = [];
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text: string) => void copied.push(text) },
  });
  onTestFinished(() => {
    Reflect.deleteProperty(navigator, "clipboard");
  });
  return copied;
}

/**
 * Start watching the event ring; the returned function gives the events logged
 * since, oldest first. By identity, not count, so neither another test's events
 * nor a full ring can shift it.
 */
export function watchEvents(): () => LogEntry[] {
  const before = new Set(recentEvents());
  return () => recentEvents().filter((entry) => !before.has(entry));
}
