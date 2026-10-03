import { useSyncExternalStore } from "react";

export type ThemePreference = "dark" | "light" | "system";

const KEY = "trackinizer.theme";
const SYSTEM_DARK = "(prefers-color-scheme: dark)";

/** Whoever shows the theme: told of every change, by a choice or by the OS in system mode. */
const listeners = new Set<() => void>();

/**
 * The preference chosen on this page, which holds whether or not storage kept
 * it: read back from a storage that refused it (a private window, a full
 * quota), Settings would show the old one, and the OS's next change apply it.
 */
let chosen: ThemePreference | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A missing or unreadable choice preserves the existing dark appearance. */
export function readTheme(): ThemePreference {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "system" ? value : "dark";
  } catch {
    return "dark";
  }
}

/** Apply a preference without changing the current page or its data. */
export function applyTheme(choice: ThemePreference): void {
  const resolved = choice === "system"
    ? (matchMedia(SYSTEM_DARK).matches ? "dark" : "light")
    : choice;
  document.documentElement.dataset.theme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  meta?.setAttribute("content", resolved === "dark" ? "#090a0b" : "#edf1f5");
}

/** Persist the browser choice and apply it immediately. */
export function chooseTheme(choice: ThemePreference): void {
  chosen = choice;
  try { localStorage.setItem(KEY, choice); } catch { /* Private storage can be unavailable. */ }
  applyTheme(choice);
  listeners.forEach((listener) => listener());
}

/**
 * The theme chosen, and the one shown (`light` or `dark`), kept current
 * wherever it is chosen: Settings and the sidebar's button show the same.
 */
export function useTheme(): { readonly choice: ThemePreference; readonly shown: "light" | "dark" } {
  const choice = useSyncExternalStore(subscribe, preference);
  const shown = useSyncExternalStore(subscribe, () => (document.documentElement.dataset.theme === "light" ? "light" : "dark"));
  return { choice, shown };
}

/** Keep system mode aligned when the OS appearance changes. */
export function installTheme(): void {
  applyTheme(readTheme());
  matchMedia(SYSTEM_DARK).addEventListener("change", () => {
    if (preference() !== "system") return;
    applyTheme("system");
    listeners.forEach((listener) => listener());
  });
}

/** The preference the page shows: the one chosen here, else the one stored. */
function preference(): ThemePreference {
  return chosen ?? readTheme();
}
