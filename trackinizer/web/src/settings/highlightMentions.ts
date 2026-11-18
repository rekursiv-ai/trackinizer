import { choosePreferences, preferences, usePreferences } from "../preferences";

/**
 * Whether the rows the assistant's answers mention light up on the canvas: one of
 * the user's preferences, on until they turn it off in Settings, and read at the
 * moment an answer arrives.
 */
export function readHighlightMentions(): boolean {
  return preferences().highlight_mentions;
}

/** The user's switch and the way to set it. */
export function useHighlightMentions(): readonly [boolean, (on: boolean) => void] {
  const on = usePreferences().highlight_mentions;
  return [on, (next) => void choosePreferences({ highlight_mentions: next })] as const;
}
