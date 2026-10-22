// A terminal's direct link: a WebRTC data channel to the machine's host, set up over the server's relay.
//
// The relay is up first and never closes. This offers a channel over it (`SIGNAL` frames); when it opens, the
// browser asks the host for the stream from its offset (`ROUTE` on the channel) and from then on output arrives
// on the channel and the relay's is ignored. Input follows once the host has answered a fence sent on the relay,
// so keystrokes never interleave across the two paths. If the channel drops, the browser asks the host to
// resume on the relay from the offset it reached. Nothing here can end a terminal: the worst outcome is staying
// on the relay.
import { decodeFrame, decodeOffset, encodeFrame, encodeRoute, Frame, Route } from "./terminalFrames";

/** How long candidates are gathered before the offer goes out with what there is. */
const GATHER_MS = 1_500;
/** How long the channel has to open (`DIRECT_OPEN_TIMEOUT_SEC`). */
const OPEN_MS = 8_000;
/** Keep offers below the server's six-per-minute limit, backing off failures to one per minute. */
const RETRY_MS = 10_000;
const RETRY_MAX_MS = 60_000;
/** The most one frame carries on the channel (`DIRECT_FRAME_BYTES`). */
const FRAME_BYTES = 16_384;

export type DirectPath = "relay" | "direct";

export type DirectOptions = {
  /** STUN URLs the server named; none means this network only. */
  readonly iceServers: readonly string[];
  /** For tests: makes the peer connection. */
  readonly peer?: (config: RTCConfiguration) => RTCPeerConnection;
  /** Sends one frame on the relay socket. */
  readonly relay: (frame: Uint8Array) => void;
  /** The stream the screen has reached. Output from either path advances it; this resets it when the host resumes elsewhere. */
  readonly stream: { offset: number };
  /** Writes output the screen counts: the callback advances `stream.offset`. */
  readonly output: (bytes: Uint8Array) => void;
  /** Writes text the screen shows that is not part of the stream. */
  readonly notice: (text: string) => void;
  /** Told when output starts or stops going direct; `note` says why it stopped. */
  readonly onPath: (path: DirectPath, note?: string) => void;
  /** The shell exited, said on the channel. */
  readonly onExit: (status?: number) => void;
};

type Signal = { readonly gen: number; readonly type: "offer" | "answer" | "abandon"; readonly sdp: string };

export class DirectChannel {
  private readonly options: DirectOptions;
  private pc: RTCPeerConnection | null = null;
  private dc: RTCDataChannel | null = null;
  private phase: "idle" | "offering" | "open" | "off" = "idle";
  private gen = 0;
  private failures = 0;
  /** Output goes by the channel: the relay's is ignored. */
  private routed = false;
  /** The host answered the fence: input may use the channel. */
  private fenced = false;
  /** The stream offset of the next byte the channel delivers. */
  private pos = 0;
  private openTimer: ReturnType<typeof setTimeout> | undefined;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(options: DirectOptions) {
    this.options = options;
  }

  /** Whether the relay's output is to be ignored, because the channel carries it. */
  get muted(): boolean {
    return this.routed;
  }

  /** Offer a channel unless one is being offered, is up, or was disposed. */
  begin(): void {
    if (this.phase !== "idle") return;
    clearTimeout(this.retryTimer);
    void this.offer();
  }

  /** The host's answer, or its refusal, as the relay delivered it. */
  signal(payload: Uint8Array): void {
    const pc = this.pc;
    let signal: Signal;
    try {
      signal = JSON.parse(new TextDecoder().decode(payload)) as Signal;
    } catch {
      return;
    }
    if (pc === null || signal.gen !== this.gen) return;
    if (signal.type === "abandon") {
      this.lost(pc);
    } else if (signal.type === "answer") {
      pc.setRemoteDescription({ type: "answer", sdp: signal.sdp }).catch(() => this.lost(pc));
    }
  }

  /** Send keystrokes on the channel; false when the relay must carry them. */
  input(bytes: Uint8Array): boolean {
    if (!this.writable()) return false;
    for (let at = 0; at < bytes.length; at += FRAME_BYTES) this.dc!.send(encodeFrame(Frame.INPUT, bytes.subarray(at, at + FRAME_BYTES)));
    return true;
  }

  /** Send the screen's size on the channel; false when the relay must carry it. */
  resize(payload: Uint8Array): boolean {
    if (!this.writable()) return false;
    this.dc!.send(encodeFrame(Frame.RESIZE, payload));
    return true;
  }

  /** The relay socket was lost or replaced: drop the channel without a word, and allow a fresh try. */
  reset(): void {
    const wasRouted = this.routed;
    this.closePeer();
    clearTimeout(this.retryTimer);
    this.failures = 0;
    if (this.phase !== "off") this.phase = "idle";
    if (wasRouted) this.options.onPath("relay");
  }

  /** The link is let go of for good. */
  dispose(): void {
    this.reset();
    this.phase = "off";
  }

  private writable(): boolean {
    return this.phase === "open" && this.fenced && this.dc?.readyState === "open";
  }

  private async offer(): Promise<void> {
    const make = this.options.peer ?? (typeof RTCPeerConnection === "undefined" ? null : (config: RTCConfiguration) => new RTCPeerConnection(config));
    if (make === null) {
      this.phase = "off";
      return;
    }
    this.phase = "offering";
    const gen = ++this.gen;
    const servers = this.options.iceServers;
    const pc = make({ iceServers: servers.length > 0 ? [{ urls: [...servers] }] : [] });
    this.pc = pc;
    const dc = pc.createDataChannel("term", { ordered: true });
    dc.binaryType = "arraybuffer";
    this.dc = dc;
    dc.onopen = () => this.opened(pc);
    dc.onmessage = (event) => this.message(pc, event.data);
    dc.onclose = () => this.lost(pc);
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed" || pc.connectionState === "closed") this.lost(pc);
    };
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await gathered(pc);
    } catch {
      this.lost(pc);
      return;
    }
    if (this.pc !== pc) return;
    const signal: Signal = { gen, type: "offer", sdp: pc.localDescription!.sdp };
    this.options.relay(encodeFrame(Frame.SIGNAL, new TextEncoder().encode(JSON.stringify(signal))));
    this.openTimer = setTimeout(() => this.lost(pc), OPEN_MS);
  }

  private opened(pc: RTCPeerConnection): void {
    if (pc !== this.pc) return;
    clearTimeout(this.openTimer);
    this.phase = "open";
    this.failures = 0;
    this.routed = true;
    this.fenced = false;
    const route = encodeFrame(Frame.ROUTE, encodeRoute(Route.DIRECT, this.options.stream.offset));
    this.dc!.send(route);
    this.options.relay(route);
    this.options.onPath("direct");
  }

  private message(pc: RTCPeerConnection, data: unknown): void {
    if (pc !== this.pc || !(data instanceof ArrayBuffer)) return;
    let frame;
    try {
      frame = decodeFrame(new Uint8Array(data));
    } catch {
      this.lost(pc);
      return;
    }
    const { stream } = this.options;
    if (frame.kind === Frame.ACK) {
      const resume = decodeOffset(frame.payload);
      if (resume > stream.offset) {
        this.options.notice("\r\n--- output lost ---\r\n");
        stream.offset = resume;
      }
      this.pos = resume;
    } else if (frame.kind === Frame.OUTPUT) {
      const end = this.pos + frame.payload.length;
      if (end > stream.offset) this.options.output(frame.payload.subarray(Math.max(0, stream.offset - this.pos)));
      this.pos = end;
    } else if (frame.kind === Frame.ROUTE) {
      this.fenced = true;
    } else if (frame.kind === Frame.EXIT) {
      this.options.onExit(frame.payload.length === 4 ? new DataView(frame.payload.buffer, frame.payload.byteOffset, 4).getUint32(0) : undefined);
    }
  }

  private lost(pc: RTCPeerConnection): void {
    if (pc !== this.pc) return;
    const wasOpen = this.phase === "open";
    this.closePeer();
    if (wasOpen) {
      this.options.relay(encodeFrame(Frame.ROUTE, encodeRoute(Route.RELAY, this.options.stream.offset)));
      this.options.onPath("relay", "Direct link lost; using the server.");
    }
    if (!wasOpen) this.failures += 1;
    const wait = Math.min(RETRY_MAX_MS, RETRY_MS * 2 ** Math.min(Math.max(0, this.failures - 1), 3));
    this.retryTimer = setTimeout(() => this.begin(), wait);
  }

  private closePeer(): void {
    clearTimeout(this.openTimer);
    const pc = this.pc;
    this.pc = null;
    this.dc = null;
    this.routed = false;
    this.fenced = false;
    if (this.phase !== "off") this.phase = "idle";
    if (pc !== null) {
      pc.onconnectionstatechange = null;
      pc.close();
    }
  }
}

/** Resolves when the peer has gathered its candidates, or after `GATHER_MS`. */
function gathered(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, GATHER_MS);
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState !== "complete") return;
      clearTimeout(timer);
      resolve();
    };
  });
}
