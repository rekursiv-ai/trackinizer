/** What a key chord asks of the terminal tile's tabs. */
export type TabChord = "new" | "close" | "next" | "previous";

type Chord = Pick<KeyboardEvent, "key" | "ctrlKey" | "shiftKey" | "altKey" | "metaKey">;

/**
 * The tab action a chord asks for, or null when it belongs to the shell.
 *
 * Ctrl+Shift+T, Ctrl+Shift+W and Ctrl+Shift+PageDown/PageUp always act (ChromeOS Terminal binds Ctrl+Shift+T the
 * same way; its libapps `terminal_emulator.js` leaves Ctrl+T/W/Tab to the shell unless `pass-ctrl-t/w/tab` is on).
 * With `passCtrl`, Ctrl+T, Ctrl+W, Ctrl+Tab and Ctrl+Shift+Tab act too, as those prefs do; without it Ctrl+W stays
 * readline's delete-word. Chrome keeps these chords in a plain browser tab; an installed app window gives them to the page.
 */
export function tabChord(event: Chord, passCtrl: boolean): TabChord | null {
  if (!event.ctrlKey || event.altKey || event.metaKey) return null;
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (event.shiftKey) {
    if (key === "t") return "new";
    if (key === "w") return "close";
    if (key === "PageDown") return "next";
    if (key === "PageUp") return "previous";
    return passCtrl && key === "Tab" ? "previous" : null;
  }
  if (!passCtrl) return null;
  if (key === "t") return "new";
  if (key === "w") return "close";
  return key === "Tab" ? "next" : null;
}
