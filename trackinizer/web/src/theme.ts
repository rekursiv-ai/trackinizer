import { useSyncExternalStore } from "react";
import { choosePreferences, preferences, subscribePreferences, usePreferences } from "./preferences";

export type ThemePreference = "dark" | "light" | "system";

const SYSTEM_DARK = "(prefers-color-scheme: dark)";

/** Whoever shows the theme: told of every change, by a choice, the server, or the OS in system mode. */
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The user's theme preference (dark until the profile brings theirs). */
export function readTheme(): ThemePreference {
  return preferences().theme;
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

/** Apply a choice at once and keep it in the user's preferences. */
export function chooseTheme(choice: ThemePreference): void {
  applyTheme(choice);
  void choosePreferences({ theme: choice });
  listeners.forEach((listener) => listener());
}

/**
 * The theme chosen, and the one shown (`light` or `dark`), kept current
 * wherever it is chosen: Settings and the sidebar's button show the same.
 */
export function useTheme(): { readonly choice: ThemePreference; readonly shown: "light" | "dark" } {
  const choice = usePreferences().theme;
  const shown = useSyncExternalStore(subscribe, () => (document.documentElement.dataset.theme === "light" ? "light" : "dark"));
  return { choice, shown };
}

/** Show the preference as it changes, and keep system mode aligned when the OS appearance changes. */
export function installTheme(): void {
  const show = () => {
    applyTheme(readTheme());
    listeners.forEach((listener) => listener());
  };
  applyTheme(readTheme());
  subscribePreferences(show);
  matchMedia(SYSTEM_DARK).addEventListener("change", () => {
    if (readTheme() === "system") show();
  });
}
