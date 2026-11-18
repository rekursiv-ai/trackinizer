import { ApiError, type CallOptions, client, send, TIMEOUT_MS } from "./client";
import { DirectChannel, type DirectPath } from "../visuals/terminalDirect";
import { decodeFrame, decodeOffset, encodeAttach, encodeFrame, encodeWinsize, Frame } from "../visuals/terminalFrames";

/** A machine the caller owns, and whether a terminal can open on it now. */
export type TerminalMachine = {
  readonly name: string;
  readonly status: string;
  readonly terminal_ready: boolean;
  /** Its owner allowed terminals on it; a machine enrolled with a key is not until they do. */
  readonly confirmed: boolean;
  /** The CLIs its facts list as installed (`claude`, `codex`, ...); absent from a server older than them. */
  readonly clis?: readonly string[];
};

/** A CLI a machine can start a chat's run in, the chat session the run re-opens, and the folder it starts in (`OpenTerminal.command`, `attach`, `cwd`). */
export type RunLaunch = { readonly command: "claude" | "codex"; readonly attach: string; readonly cwd?: string };

/** A terminal that is open now, the machine it runs on, and the view that opened it. */
export type LiveTerminal = { readonly terminal_id: string; readonly machine: string; readonly surface?: string | null };

/** The caller's machines, newest ready first, and their live terminals opened from the asked station, oldest first. */
export type TerminalMachines = { readonly machines: readonly TerminalMachine[]; readonly terminals?: readonly LiveTerminal[] };

/** What opening a terminal returns: its id, the one-use ticket, and the STUN servers a direct link may use. */
export type OpenedTerminal = { readonly terminal_id: string; readonly ticket: string; readonly ice_servers?: readonly string[] };

/** The caller's machines, newest first; `workspace` asks for that station's live terminals too. */
export async function listTerminalMachines(workspace?: string, { signal }: CallOptions = {}): Promise<TerminalMachines> {
  return send(TIMEOUT_MS.read, signal, (signal) => client.GET("/api/terminals/machines", {
    params: { query: { workspace } }, signal,
  }));
}

/**
 * Open a terminal on `machine`, from `workspace`'s station, for the view named `surface`. Only a click calls it.
 * With `launch`, the machine starts that CLI's run in place of a login shell, re-opening the chat session it names,
 * in `launch.cwd` when given (else the home directory); the server derives the run's name.
 */
export async function openTerminal(machine: string, workspace: string | null, rows: number, cols: number, surface: string | null,
  launch?: RunLaunch): Promise<OpenedTerminal> {
  return send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/terminals", {
    body: { machine, workspace, rows, cols, surface, ...launch }, signal,
  }));
}

/** A fresh one-use ticket to attach to a live terminal again, and the STUN servers a direct link may use. */
export async function attachTerminal(id: string): Promise<{ readonly ticket: string; readonly ice_servers: readonly string[] }> {
  const { ticket, ice_servers = [] } = await send(TIMEOUT_MS.write, undefined, (signal) => client.POST("/api/terminals/{terminal_id}/attach", {
    params: { path: { terminal_id: id } }, signal,
  }));
  return { ticket, ice_servers };
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
  /** Told, in ms, how long the first INPUT frame still unanswered waited for the next OUTPUT, and which path carried the echo. */
  readonly onEcho?: (ms: number, path: DirectPath) => void;
  /** The STUN servers a direct link may use; none keeps it to one network. */
  readonly iceServers?: readonly string[];
  /** Told when output starts going direct, and when it falls back to the relay (`note` says why). */
  readonly onPath?: (path: DirectPath, note?: string) => void;
  /** For tests: makes the peer connection. */
  readonly peer?: (config: RTCConfiguration) => RTCPeerConnection;
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

const encoder = new TextEncoder();

/**
 * Attach to a terminal: send `ATTACH` (ticket and the offset to resume at) first, relay frames, and
 * on a lost socket attach again with a fresh ticket from the last offset, 0.5 to 8 s apart for up to
 * 60 s. The ticket never rides the URL. Once attached it offers a direct link over the socket
 * (`terminalDirect.ts`); the socket stays up, so the link can fall back to it at any moment.
 */
export function connectTerminal(options: LinkOptions): TerminalLink {
  const { id, onOutput, onStatus, socket = (url) => new WebSocket(url), size, onEcho, onPath, peer } = options;
  const fresh = options.fresh ?? (async (given: string) => (await attachTerminal(given)).ticket);
  let ws: WebSocket | null = null;
  // When the first INPUT frame not yet answered went out. Dropped at every ACK and close, so output replayed after
  // an attach is never taken for an echo.
  let typedAt: number | null = null;
  let ticket: string | null = options.ticket;
  const stream = { offset: 0 };
  let attempt = 0;
  let lostAt = 0;
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const finish = (status: LinkStatus) => {
    closed = true;
    clearTimeout(timer);
    direct.dispose();
    onStatus(status);
  };

  const send = (frame: Uint8Array) => {
    if (ws?.readyState === WebSocket.OPEN) ws.send(frame);
  };

  const deliver = (bytes: Uint8Array, path: DirectPath) => {
    stream.offset += bytes.length;
    if (typedAt !== null) {
      onEcho?.(performance.now() - typedAt, path);
      typedAt = null;
    }
    onOutput(bytes);
  };

  const direct: DirectChannel = new DirectChannel({
    iceServers: options.iceServers ?? [], ...(peer ? { peer } : {}), relay: send, stream,
    output: (bytes) => deliver(bytes, "direct"),
    notice: (text) => onOutput(encoder.encode(text)),
    onPath: (path, note) => {
      typedAt = null;
      onPath?.(path, note);
    },
    onExit: (code) => finish({ state: "ended", note: "The shell exited.", ...(code === undefined ? {} : { exit: code }) }),
  });

  function dial(): void {
    if (closed || ticket === null) return;
    const mine = ticket;
    ticket = null;
    const next = socket(socketUrl(id));
    next.binaryType = "arraybuffer";
    ws = next;
    // The first ACK of this socket starts a direct link; one after it is the host resuming on the relay.
    let attached = false;
    onStatus({ state: attempt === 0 ? "connecting" : "reconnecting" });
    next.onopen = () => next.send(encodeFrame(Frame.ATTACH, encodeAttach(mine, stream.offset)));
    next.onmessage = (event: MessageEvent) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      const frame = decodeFrame(new Uint8Array(event.data));
      if (frame.kind === Frame.ACK) {
        const resume = decodeOffset(frame.payload);
        if (attached && resume > stream.offset) onOutput(encoder.encode(LOST_OUTPUT));
        stream.offset = resume;
        typedAt = null;
        attempt = 0;
        lostAt = 0;
        onStatus({ state: "live" });
        if (size) {
          const { rows, cols } = size();
          send(encodeFrame(Frame.RESIZE, encodeWinsize(rows, cols)));
        }
        if (!attached) direct.begin();
        attached = true;
      } else if (frame.kind === Frame.OUTPUT) {
        if (!direct.muted) deliver(frame.payload, "relay");
      } else if (frame.kind === Frame.SIGNAL) {
        direct.signal(frame.payload);
      } else if (frame.kind === Frame.EXIT) {
        const code = frame.payload.length === 4 ? new DataView(frame.payload.buffer, frame.payload.byteOffset, 4).getUint32(0) : undefined;
        finish({ state: "ended", note: "The shell exited.", ...(code === undefined ? {} : { exit: code }) });
      }
    };
    next.onclose = (event: CloseEvent) => {
      if (closed || ws !== next) return;
      typedAt = null;
      direct.reset();
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
      typedAt ??= performance.now();
      if (direct.input(bytes)) return;
      for (let at = 0; at < bytes.length; at += MAX_INPUT_FRAME) send(encodeFrame(Frame.INPUT, bytes.subarray(at, at + MAX_INPUT_FRAME)));
    },
    resize: (rows, cols) => {
      const payload = encodeWinsize(rows, cols);
      if (!direct.resize(payload)) send(encodeFrame(Frame.RESIZE, payload));
    },
    stop: () => send(encodeFrame(Frame.STOP)),
    dispose: () => {
      closed = true;
      clearTimeout(timer);
      direct.dispose();
      ws?.close(1000);
    },
  };
}

/** What the screen shows where a host resumed past the output the screen reached. */
const LOST_OUTPUT = "\r\n--- output lost ---\r\n";
