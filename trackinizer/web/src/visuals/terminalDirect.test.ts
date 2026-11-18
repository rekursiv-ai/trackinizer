import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DirectChannel, type DirectOptions } from "./terminalDirect";
import { decodeFrame, decodeRoute, encodeFrame, encodeRoute, Frame, Route } from "./terminalFrames";

class FakeChannel {
  binaryType = "blob";
  readyState = "open";
  sent: Uint8Array[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  send(data: Uint8Array) { this.sent.push(data); }
  close() { this.closed = true; }
  frame(kind: number, payload: Uint8Array = new Uint8Array()) {
    const bytes = encodeFrame(kind as 1, payload);
    this.onmessage?.({ data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) });
  }
  ack(offset: number) {
    const payload = new Uint8Array(8);
    new DataView(payload.buffer).setBigUint64(0, BigInt(offset));
    this.frame(Frame.ACK, payload);
  }
}

class FakePeer {
  static all: FakePeer[] = [];
  static failNext = false;
  readonly config: RTCConfiguration;
  readonly channel = new FakeChannel();
  iceGatheringState = "gathering";
  connectionState = "new";
  localDescription: { sdp: string } | null = null;
  remote: { type: string; sdp: string } | null = null;
  closed = false;
  fail = false;
  onicegatheringstatechange: (() => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  constructor(config: RTCConfiguration) { this.config = config; this.fail = FakePeer.failNext; FakePeer.failNext = false; FakePeer.all.push(this); }
  createDataChannel() { return this.channel; }
  createOffer() { return Promise.resolve({ type: "offer", sdp: "offer-sdp" }); }
  setLocalDescription() {
    if (this.fail) return Promise.reject(new Error("no"));
    this.localDescription = { sdp: "offer-sdp" };
    return Promise.resolve();
  }
  setRemoteDescription(description: { type: string; sdp: string }) { this.remote = description; return Promise.resolve(); }
  close() { this.closed = true; }
  gathered() { this.iceGatheringState = "complete"; this.onicegatheringstatechange?.(); }
  state(next: string) { this.connectionState = next; this.onconnectionstatechange?.(); }
}

type Setup = ReturnType<typeof setup>;

function setup(iceServers: readonly string[] = ["stun:s.test:3478"], extra: Partial<DirectOptions> = {}) {
  FakePeer.all = [];
  const relay: Uint8Array[] = [];
  const stream = { offset: 0 };
  const out: number[][] = [];
  const notices: string[] = [];
  const paths: Array<[string, string | undefined]> = [];
  const exits: Array<number | undefined> = [];
  const direct = new DirectChannel({
    iceServers,
    peer: (config) => new FakePeer(config) as unknown as RTCPeerConnection,
    relay: (frame) => relay.push(frame),
    stream,
    output: (bytes) => { stream.offset += bytes.length; out.push([...bytes]); },
    notice: (text) => notices.push(text),
    onPath: (path, note) => paths.push([path, note]),
    onExit: (status) => exits.push(status),
    ...extra,
  });
  const peer = () => FakePeer.all.at(-1)!;
  const kinds = () => relay.map((frame) => decodeFrame(frame).kind);
  const signals = () => relay.map((frame) => decodeFrame(frame)).filter((frame) => frame.kind === Frame.SIGNAL)
    .map((frame) => JSON.parse(new TextDecoder().decode(frame.payload)) as { gen: number; type: string; sdp: string });
  return { direct, relay, stream, out, notices, paths, exits, peer, kinds, signals };
}

/** Offer, answer, open: the channel is up and the browser has said so on both paths. */
async function opened(world: Setup) {
  world.direct.begin();
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);
  world.direct.signal(new TextEncoder().encode(JSON.stringify({ gen: 1, type: "answer", sdp: "answer-sdp" })));
  world.peer().channel.onopen?.();
}

function fenceAck(world: Setup) {
  world.peer().channel.frame(Frame.ROUTE, encodeRoute(Route.DIRECT, 0));
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("the offer goes out on the relay once candidates are gathered, over the servers it was given", async () => {
  const world = setup(["stun:a.test:3478", "stun:b.test"]);
  world.direct.begin();
  await vi.advanceTimersByTimeAsync(0);
  expect(world.signals()).toEqual([]);
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);

  expect(world.signals()).toEqual([{ gen: 1, type: "offer", sdp: "offer-sdp" }]);
  expect(world.peer().config).toEqual({ iceServers: [{ urls: ["stun:a.test:3478", "stun:b.test"] }] });
  expect(world.peer().channel.binaryType).toBe("arraybuffer");
});

test("with no server named the configuration names none, so the browser adds none", async () => {
  const world = setup([]);
  world.direct.begin();
  await vi.advanceTimersByTimeAsync(0);

  expect(world.peer().config).toEqual({ iceServers: [] });
});

test("gathering that never completes does not hold the offer back past 1.5 s", async () => {
  const world = setup();
  world.direct.begin();
  await vi.advanceTimersByTimeAsync(1_499);
  expect(world.signals()).toEqual([]);
  await vi.advanceTimersByTimeAsync(1);

  expect(world.signals()).toHaveLength(1);
});

test("an answer is applied, one for another generation is not, and begin twice makes one peer", async () => {
  const world = setup();
  world.direct.begin();
  world.direct.begin();
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);
  world.direct.signal(new TextEncoder().encode(JSON.stringify({ gen: 9, type: "answer", sdp: "stale" })));
  expect(world.peer().remote).toBeNull();
  world.direct.signal(new TextEncoder().encode(JSON.stringify({ gen: 1, type: "answer", sdp: "answer-sdp" })));

  expect(world.peer().remote).toEqual({ type: "answer", sdp: "answer-sdp" });
  expect(FakePeer.all).toHaveLength(1);
});

test("successful links can recover after more than two drops", async () => {
  const world = setup();
  await opened(world);
  for (let drop = 0; drop < 4; drop++) {
    world.peer().channel.onclose?.();
    await vi.advanceTimersByTimeAsync(10_000);
    world.peer().gathered();
    await vi.advanceTimersByTimeAsync(0);
    world.peer().channel.onopen?.();
  }

  expect(world.signals()).toHaveLength(5);
  expect(world.direct.muted).toBe(true);
  world.direct.dispose();
});

test("an abandon closes the peer and retries while the browser stays on the relay", async () => {
  const world = setup();
  world.direct.begin();
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);
  world.direct.signal(new TextEncoder().encode(JSON.stringify({ gen: 1, type: "abandon", sdp: "" })));
  await vi.advanceTimersByTimeAsync(60_000);

  expect(FakePeer.all[0]!.closed).toBe(true);
  expect(FakePeer.all.length).toBeGreaterThan(1);
  expect(world.paths).toEqual([]);
  world.direct.dispose();
});

test("when the channel opens the browser asks for the route on the channel, then fences on the relay", async () => {
  const world = setup();
  world.stream.offset = 7;
  await opened(world);

  const onChannel = decodeFrame(world.peer().channel.sent[0]!);
  expect([onChannel.kind, decodeRoute(onChannel.payload)]).toEqual([Frame.ROUTE, { route: Route.DIRECT, offset: 7 }]);
  const onRelay = decodeFrame(world.relay.at(-1)!);
  expect([onRelay.kind, decodeRoute(onRelay.payload)]).toEqual([Frame.ROUTE, { route: Route.DIRECT, offset: 7 }]);
  expect(world.direct.muted).toBe(true);
  expect(world.paths).toEqual([["direct", undefined]]);
});

test("the relay's output is ignored from the moment the route is asked for, and not before", async () => {
  const world = setup();
  world.direct.begin();
  expect(world.direct.muted).toBe(false);
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);
  world.direct.signal(new TextEncoder().encode(JSON.stringify({ gen: 1, type: "answer", sdp: "a" })));
  expect(world.direct.muted).toBe(false);
  world.peer().channel.onopen?.();

  expect(world.direct.muted).toBe(true);
});

test("output on the channel is trimmed to what the screen has not shown, and never repeated", async () => {
  const world = setup();
  world.stream.offset = 10;
  await opened(world);
  const channel = world.peer().channel;

  channel.ack(6);
  channel.frame(Frame.OUTPUT, Uint8Array.from({ length: 8 }, (_, at) => at));
  channel.frame(Frame.OUTPUT, new Uint8Array([100, 101]));
  channel.frame(Frame.OUTPUT, Uint8Array.from([1, 2, 3]));

  expect(world.out).toEqual([[4, 5, 6, 7], [100, 101], [1, 2, 3]]);
  expect(world.stream.offset).toBe(19);
});

test("output the host no longer has is marked lost and the offset jumps to where it resumes", async () => {
  const world = setup();
  world.stream.offset = 10;
  await opened(world);

  world.peer().channel.ack(25);
  world.peer().channel.frame(Frame.OUTPUT, new Uint8Array([9]));

  expect(world.notices).toEqual(["\r\n--- output lost ---\r\n"]);
  expect(world.stream.offset).toBe(26);
  expect(world.out).toEqual([[9]]);
});

test("input stays on the relay until the host answers the fence, then goes on the channel in 16 KiB frames", async () => {
  const world = setup();
  await opened(world);
  expect(world.direct.input(new Uint8Array([1]))).toBe(false);
  expect(world.direct.resize(new Uint8Array([0, 1, 0, 2]))).toBe(false);
  fenceAck(world);

  const paste = new Uint8Array(16_384 * 2 + 5).map((_, at) => at % 251);
  expect(world.direct.input(paste)).toBe(true);
  expect(world.direct.resize(new Uint8Array([0, 1, 0, 2]))).toBe(true);

  const frames = world.peer().channel.sent.slice(1).map((sent) => decodeFrame(sent));
  expect(frames.map((frame) => [frame.kind, frame.payload.length])).toEqual([
    [Frame.INPUT, 16_384], [Frame.INPUT, 16_384], [Frame.INPUT, 5], [Frame.RESIZE, 4],
  ]);
});

test("a channel that does not open in 8 s retries with backoff while output stays on the relay", async () => {
  const world = setup();
  world.direct.begin();
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(8_000);

  expect(FakePeer.all[0]!.closed).toBe(true);
  expect(world.paths).toEqual([]);
  expect(world.kinds().filter((kind) => kind === Frame.ROUTE)).toEqual([]);
  await vi.advanceTimersByTimeAsync(9_999);
  expect(FakePeer.all).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(FakePeer.all).toHaveLength(2);
  world.peer().gathered();
  await vi.advanceTimersByTimeAsync(0);
  expect(world.signals().map((signal) => signal.gen)).toEqual([1, 2]);
  await vi.advanceTimersByTimeAsync(8_000 + 60_000);
  expect(FakePeer.all.length).toBeGreaterThan(2);
  world.direct.dispose();
});

test("a peer that cannot make an offer is closed, says nothing on the relay, and is retried", async () => {
  const world = setup();
  FakePeer.failNext = true;
  world.direct.begin();
  await vi.advanceTimersByTimeAsync(0);

  expect(FakePeer.all[0]!.closed).toBe(true);
  expect(world.signals()).toEqual([]);
  await vi.advanceTimersByTimeAsync(10_000);
  expect(FakePeer.all).toHaveLength(2);
});

test("a channel that closes after it opened returns output to the relay from the offset the screen reached, and says so", async () => {
  const world = setup();
  await opened(world);
  world.peer().channel.ack(0);
  world.peer().channel.frame(Frame.OUTPUT, new Uint8Array(5));

  world.peer().channel.onclose?.();

  const last = decodeFrame(world.relay.at(-1)!);
  expect([last.kind, decodeRoute(last.payload)]).toEqual([Frame.ROUTE, { route: Route.RELAY, offset: 5 }]);
  expect(world.direct.muted).toBe(false);
  expect(world.paths.at(-1)).toEqual(["relay", "Direct link lost; using the server."]);
  expect(world.peer().closed).toBe(true);
  expect(world.direct.input(new Uint8Array([1]))).toBe(false);
});

test("a failed connection is a lost one, a closed one after that is not reported twice", async () => {
  const world = setup();
  await opened(world);

  world.peer().state("failed");
  world.peer().state("closed");

  expect(world.paths.filter(([path]) => path === "relay")).toHaveLength(1);
  expect(world.kinds().filter((kind) => kind === Frame.ROUTE)).toHaveLength(2);
});

test("after a drop it retries after 10 s, from a clean state", async () => {
  const world = setup();
  await opened(world);
  world.peer().channel.onclose?.();
  await vi.advanceTimersByTimeAsync(9_999);
  expect(FakePeer.all).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(1);

  expect(FakePeer.all).toHaveLength(2);
  expect(world.direct.muted).toBe(false);
});

test("the shell's exit on the channel is passed on with its status", async () => {
  const world = setup();
  await opened(world);

  world.peer().channel.frame(Frame.EXIT, new Uint8Array([0, 0, 0, 3]));
  world.peer().channel.frame(Frame.EXIT, new Uint8Array());

  expect(world.exits).toEqual([3, undefined]);
});

test("a malformed or textual message on the channel is a lost channel", async () => {
  const world = setup();
  await opened(world);

  world.peer().channel.onmessage?.({ data: "text" });
  expect(world.paths.filter(([path]) => path === "relay")).toHaveLength(0);
  world.peer().channel.onmessage?.({ data: new Uint8Array([2, 0]).buffer });

  expect(world.paths.at(-1)?.[0]).toBe("relay");
});

test("a reset closes the peer without a word on the relay and clears the tries and the timers", async () => {
  const world = setup();
  await opened(world);
  const before = world.relay.length;

  world.direct.reset();
  await vi.advanceTimersByTimeAsync(60_000);

  expect(world.peer().closed).toBe(true);
  expect(world.relay).toHaveLength(before);
  expect(world.direct.muted).toBe(false);
  expect(world.paths.at(-1)).toEqual(["relay", undefined]);
  expect(FakePeer.all).toHaveLength(1);
  world.direct.begin();
  expect(FakePeer.all).toHaveLength(2);
});

test("a disposed channel never starts again", async () => {
  const world = setup();
  await opened(world);

  world.direct.dispose();
  world.direct.begin();
  await vi.advanceTimersByTimeAsync(60_000);

  expect(FakePeer.all).toHaveLength(1);
  expect(world.peer().closed).toBe(true);
});

test("a browser with no WebRTC stays on the relay without error", () => {
  vi.stubGlobal("RTCPeerConnection", undefined);
  const direct = new DirectChannel({
    iceServers: [], relay: () => {}, stream: { offset: 0 }, output: () => {}, notice: () => {}, onPath: () => {}, onExit: () => {},
  });

  direct.begin();

  expect(direct.muted).toBe(false);
  vi.unstubAllGlobals();
});
