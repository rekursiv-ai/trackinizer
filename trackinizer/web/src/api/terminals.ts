import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import { decodeFrame, decodeOffset, encodeAttach, encodeFrame, encodeWinsize, Frame } from "../visuals/terminalFrames";

/** A machine the caller owns, and whether a terminal can open on it now. */
export type TerminalMachine = {
  readonly name: string;
  readonly status: string;
  readonly terminal_ready: boolean;
  /** Its owner allowed terminals on it; a machine enrolled with a key is not until they do. */
  readonly confirmed: boolean;
};

/** A terminal that is open now, and the machine it runs on. */
export type LiveTerminal = { readonly terminal_id: string; readonly session_id: string; readonly machine: string };

/** The caller's machines, newest ready first, and their live terminal opened from the asked station. */
export type TerminalMachines = { readonly machines: readonly TerminalMachine[]; readonly live?: LiveTerminal | null };

/** What opening a terminal returns: its id, the session that records it, and the one-use ticket. */
export type OpenedTerminal = { readonly terminal_id: string; readonly session_id: string; readonly ticket: string };

/** The caller's machines, newest first; `workspace` asks for that station's live terminal too. */
export async function listTerminalMachines(workspace?: string, { signal }: CallOptions = {}): Promise<TerminalMachines> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/terminals/machines", {
    params: { query: { workspace } }, signal,
  }));
}

/** Open a terminal on `machine`, from `workspace`'s station. Only a click calls it. */
export async function openTerminal(machine: string, workspace: string | null, rows: number, cols: number): Promise<OpenedTerminal> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/terminals", {
    body: { machine, workspace, rows, cols }, signal,
  }));
}

/** A fresh one-use ticket to attach to a live terminal again. */
export async function attachTerminal(id: string): Promise<string> {
  const { ticket } = await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/terminals/{terminal_id}/attach", {
    params: { path: { terminal_id: id } }, signal,
  }));
  return ticket;
}

/** Allow terminals on one of the user's machines (cookie only: no key can). */
export async function confirmTerminalMachine(name: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/terminals/machines/{name}/confirm", {
    params: { path: { name } }, signal,
  }));
}

/** Stop the terminal's shell. */
export async function closeTerminal(id: string): Promise<void> {
  await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/terminals/{terminal_id}/close", {
    params: { path: { terminal_id: id } }, signal,
  }));
}

/** Where a terminal's link stands. `ended` and `taken` and `refused` are final. */
export type LinkStatus =
  | { readonly state: "connecting" | "live" | "reconnecting" }
  | { readonly state: "ended"; readonly note: string; readonly exit?: number }
  | { readonly state: "taken" | "refused"; readonly note: string };

/** What a close code says, for the codes the server sends (design B2 section 1). */
const FINAL: Readonly<Record<number, LinkStatus>> = {
  4000: { state: "ended", note: "The terminal ended." },
  4001: { state: "taken", note: "Another window took this terminal over." },
  4002: { state: "ended", note: "Closed after 60 minutes idle." },
  4003: { state: "ended", note: "Closed at the 24 hour limit." },
  4403: { state: "refused", note: "The server refused this terminal." },
  4410: { state: "ended", note: "The machine was revoked." },
  // The host is gone and the server dropped the terminal, so an attach would be a 404: nothing to retry.
  4004: { state: "ended", note: "The machine disconnected." },
  4429: { state: "refused", note: "Terminal limit reached; close one." },
};

/** Waits before each retry, then the last until `GIVE_UP_MS` have passed. */
export const BACKOFF_MS = [500, 1000, 2000, 4000, 8000] as const;
export const GIVE_UP_MS = 60_000;

/** The most one INPUT frame may carry (`MAX_INPUT_FRAME_BYTES`). */
export const MAX_INPUT_FRAME = 65_536;

export type LinkOptions = {
  readonly id: string;
  readonly ticket: string;
  readonly onOutput: (bytes: Uint8Array) => void;
  readonly onStatus: (status: LinkStatus) => void;
  /** For tests: the socket and ticket sources. */
  readonly socket?: (url: string) => WebSocket;
  readonly fresh?: (id: string) => Promise<string>;
  /** The screen's size now: sent as a RESIZE after every ACK, so the shell has the real size whatever size it opened at. */
  readonly size?: () => { readonly rows: number; readonly cols: number };
};

/** The live link to one terminal. */
export type TerminalLink = {
  readonly input: (bytes: Uint8Array) => void;
  readonly resize: (rows: number, cols: number) => void;
  /** Stop the shell. */
  readonly stop: () => void;
  /** Let go of the socket and leave the shell running, for a reload or a flip that unmounts. */
  readonly dispose: () => void;
};

function socketUrl(id: string): string {
  const url = new URL(`/api/terminals/${encodeURIComponent(id)}/ws`, globalThis.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

/**
 * Attach to a terminal: send `ATTACH` (ticket and the offset to resume at) first, relay frames, and
 * on a lost socket attach again with a fresh ticket from the last offset, 0.5 to 8 s apart for up to
 * 60 s. The ticket never rides the URL.
 */
export function connectTerminal(options: LinkOptions): TerminalLink {
  const { id, onOutput, onStatus, socket = (url) => new WebSocket(url), fresh = attachTerminal, size } = options;
  let ws: WebSocket | null = null;
  let ticket: string | null = options.ticket;
  let offset = 0;
  let attempt = 0;
  let lostAt = 0;
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const finish = (status: LinkStatus) => {
    closed = true;
    clearTimeout(timer);
    onStatus(status);
  };

  const send = (frame: Uint8Array) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(frame);
  };

  function dial(): void {
    if (closed || ticket === null) return;
    const mine = ticket;
    ticket = null;
    const next = socket(socketUrl(id));
    next.binaryType = "arraybuffer";
    ws = next;
    onStatus({ state: attempt === 0 ? "connecting" : "reconnecting" });
    next.onopen = () => next.send(encodeFrame(Frame.ATTACH, encodeAttach(mine, offset)));
    next.onmessage = (event: MessageEvent) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      const frame = decodeFrame(new Uint8Array(event.data));
      if (frame.kind === Frame.ACK) {
        offset = decodeOffset(frame.payload);
        attempt = 0;
        lostAt = 0;
        onStatus({ state: "live" });
        if (size) {
          const { rows, cols } = size();
          send(encodeFrame(Frame.RESIZE, encodeWinsize(rows, cols)));
        }
      } else if (frame.kind === Frame.OUTPUT) {
        offset += frame.payload.length;
        onOutput(frame.payload);
      } else if (frame.kind === Frame.EXIT) {
        const code = frame.payload.length === 4 ? new DataView(frame.payload.buffer, frame.payload.byteOffset, 4).getUint32(0) : undefined;
        finish({ state: "ended", note: "The shell exited.", ...(code === undefined ? {} : { exit: code }) });
      }
    };
    next.onclose = (event: CloseEvent) => {
      if (closed || ws !== next) return;
      const known = FINAL[event.code];
      if (known) finish(known);
      else retry();
    };
  }

  function retry(): void {
    if (lostAt === 0) lostAt = Date.now();
    if (Date.now() - lostAt >= GIVE_UP_MS) {
      finish({ state: "ended", note: "Lost the connection to the terminal." });
      return;
    }
    onStatus({ state: "reconnecting" });
    const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]!;
    attempt += 1;
    timer = setTimeout(() => {
      fresh(id).then((next) => { ticket = next; dial(); }, (error: unknown) => {
        // The terminal no longer exists: waiting will not bring it back.
        if (error instanceof ApiError && (error.status === 404 || error.status === 410)) finish({ state: "ended", note: "The terminal is gone." });
        else retry();
      });
    }, wait);
  }

  dial();
  return {
    // A paste arrives as one string: the server refuses an INPUT frame over 64 KiB, so it goes in frames of at most that.
    input: (bytes) => {
      for (let at = 0; at < bytes.length; at += MAX_INPUT_FRAME) send(encodeFrame(Frame.INPUT, bytes.subarray(at, at + MAX_INPUT_FRAME)));
    },
    resize: (rows, cols) => send(encodeFrame(Frame.RESIZE, encodeWinsize(rows, cols))),
    stop: () => send(encodeFrame(Frame.STOP)),
    dispose: () => {
      closed = true;
      clearTimeout(timer);
      ws?.close(1000);
    },
  };
}
