import type { IClipboardProvider } from "@xterm/addon-clipboard";

/** How long after the user's last key or click in the terminal a program may still write the clipboard. */
export const COPY_WINDOW_MS = 5_000;

/** The most a program may put on the clipboard at once, in bytes of UTF-8. */
export const MAX_COPY_BYTES = 100 * 1024;

export type ClipboardGate = {
  /** When the user last typed or clicked in this terminal (`Date.now()`), or 0. */
  readonly lastInput: () => number;
  /** Told when a write went through, to show "Copied from terminal". */
  readonly copied: () => void;
};

/**
 * OSC 52 for the terminal: a program may put text on the user's clipboard (tmux's copy reaches the
 * laptop) only within `COPY_WINDOW_MS` of the user's own key or click, at most `MAX_COPY_BYTES`; any
 * other write is ignored. `readText` answers empty whatever the program asks, so a program can never
 * read the clipboard.
 */
export function gatedClipboard({ lastInput, copied }: ClipboardGate): IClipboardProvider {
  return {
    readText: () => "",
    writeText: (_selection, text) => {
      if (Date.now() - lastInput() > COPY_WINDOW_MS || new TextEncoder().encode(text).length > MAX_COPY_BYTES) return;
      return navigator.clipboard.writeText(text).then(copied, () => undefined);
    },
  };
}
