import { useSyncExternalStore } from "react";
import { choosePreferences, type Preferences, preferences, subscribePreferences, usePreferences } from "./preferences";

export type ThemePreference = "dark" | "light" | "system";

/** The app's look: the default, which the theme colours, or a fixed skin (skins.css), which ignores it. */
export type Skin = Preferences["skin"];

/** Every skin, as Settings' Appearance offers them, in order. */
export const SKINS: readonly { readonly skin: Skin; readonly label: string }[] = [
  { skin: "default", label: "Default" },
  { skin: "retro", label: "Retro 70s" },
  { skin: "scifi", label: "Sci-fi" },
  { skin: "dos", label: "DOS 80s" },
  { skin: "console", label: "8-bit Console" },
  { skin: "matrix", label: "Matrix" },
  { skin: "hacker", label: "Hacker" },
  { skin: "minimal", label: "Minimalist" },
  { skin: "brutal", label: "Brutalist" },
];

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

/** Show `skin`: a fixed skin is the root's `data-skin`, which skins.css colours over any theme; the default is none. */
export function applySkin(skin: Skin): void {
  if (skin === "default") delete document.documentElement.dataset.skin;
  else document.documentElement.dataset.skin = skin;
}

/** Apply a choice at once and keep it in the user's preferences. */
export function chooseTheme(choice: ThemePreference): void {
  applyTheme(choice);
  void choosePreferences({ theme: choice });
  listeners.forEach((listener) => listener());
}

/** Show a skin at once and keep it in the user's preferences. */
export function chooseSkin(skin: Skin): void {
  applySkin(skin);
  void choosePreferences({ skin });
}

/**
 * The theme chosen, and the one shown (`light` or `dark`), kept current
 * wherever it is chosen: Settings and the sidebar's theme button show the same.
 */
export function useTheme(): { readonly choice: ThemePreference; readonly shown: "light" | "dark" } {
  const choice = usePreferences().theme;
  const shown = useSyncExternalStore(subscribe, () => (document.documentElement.dataset.theme === "light" ? "light" : "dark"));
  return { choice, shown };
}

/** Show the preferences as they change, and keep system mode aligned when the OS appearance changes. */
export function installTheme(): void {
  const show = () => {
    applyTheme(readTheme());
    applySkin(preferences().skin);
    listeners.forEach((listener) => listener());
  };
  applyTheme(readTheme());
  applySkin(preferences().skin);
  subscribePreferences(show);
  matchMedia(SYSTEM_DARK).addEventListener("change", () => {
    if (readTheme() === "system") show();
  });
}
