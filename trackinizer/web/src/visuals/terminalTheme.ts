import type { ITerminalOptions, ITheme } from "@xterm/xterm";

/** GNOME Terminal 3.58's 16 colours, the palette docs/retro_ui.md names, and the only place it is kept. */
export const TERMINAL_THEME: ITheme = {
  foreground: "#00ff00", background: "#000000", cursor: "#00ff00",
  black: "#171421", red: "#c01c28", green: "#26a269", yellow: "#a2734c",
  blue: "#12488b", magenta: "#a347ba", cyan: "#2aa1b3", white: "#d0cfcc",
  brightBlack: "#5e5c64", brightRed: "#f66151", brightGreen: "#33d17a", brightYellow: "#e9ad0c",
  brightBlue: "#2a7bde", brightMagenta: "#c061cb", brightCyan: "#33c7de", brightWhite: "#ffffff",
};

/** The xterm options: bold in bright colours, a blinking block, 50,000 lines, no bell, DejaVu Sans Mono. */
export function terminalOptions(fontSize: string): ITerminalOptions {
  const px = /^(\d+(?:\.\d+)?)(px|pt)$/.exec(fontSize);
  const size = px ? Number(px[1]) * (px[2] === "pt" ? 4 / 3 : 1) : 13;
  return {
    theme: TERMINAL_THEME, drawBoldTextInBrightColors: true, cursorBlink: true, cursorStyle: "block",
    scrollback: 50_000, fontFamily: '"DejaVu Sans Mono", monospace', fontSize: size,
  };
}
