import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Frame, decodeFrame, encodeFrame } from "../visuals/terminalFrames";
import { ApiError } from "./client";
import { connectTerminal, listTerminalMachines, type LinkStatus, openTerminal } from "./terminals";
import { stubFetch } from "./testing";

class FakeSocket {
  static all: FakeSocket[] = [];
  binaryType = "blob";
  readyState = 1;
  sent: Uint8Array[] = [];
  closedWith: number | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  readonly url: string;
  constructor(url: string) { this.url = url; FakeSocket.all.push(this); }
  send(data: Uint8Array) { this.sent.push(data); }
  close(code: number) { this.closedWith = code; }
  open() { this.onopen?.(); }
  frame(kind: number, payload: Uint8Array = new Uint8Array()) {
    const bytes = encodeFrame(kind as 1, payload);
    this.onmessage?.({ data: bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) } as MessageEvent);
  }
  ack(offset: number) {
    const payload = new Uint8Array(8);
    new DataView(payload.buffer).setBigUint64(0, BigInt(offset));
    this.frame(Frame.ACK, payload);
  }
  drop(code: number) { this.onclose?.({ code } as CloseEvent); }
}

function start(fresh: (id: string) => Promise<string> = () => Promise.resolve("T".repeat(43))) {
  const statuses: LinkStatus[] = [];
  const output: number[] = [];
  const link = connectTerminal({
    id: "t1", ticket: "A".repeat(43), onOutput: (bytes) => output.push(bytes.length), onStatus: (status) => statuses.push(status),
    socket: (url) => new FakeSocket(url) as unknown as WebSocket, fresh,
  });
  return { link, statuses, output, socket: () => FakeSocket.all.at(-1)! };
}

beforeEach(() => { FakeSocket.all = []; vi.useFakeTimers(); });
afterEach(() => vi.useRealTimers());

test("ATTACH is the first frame, carries the ticket and offset, and the ticket is not in the URL", () => {
  const { socket } = start();
  socket().open();
  const frame = decodeFrame(socket().sent[0]!);
  expect(frame.kind).toBe(Frame.ATTACH);
  expect(new TextDecoder().decode(frame.payload.subarray(0, 43))).toBe("A".repeat(43));
  expect(socket().url).toBe("ws://localhost:3000/api/terminals/t1/ws");
});

test("input and resize go out as frames", () => {
  const { link, socket } = start();
  socket().open();
  link.input(new Uint8Array([104]));
  link.resize(30, 100);
  expect(socket().sent.slice(1).map((sent) => decodeFrame(sent).kind)).toEqual([Frame.INPUT, Frame.RESIZE]);
});

test("a lost socket attaches again with a fresh ticket from the last offset", async () => {
  const { socket, output, statuses } = start();
  socket().open();
  socket().ack(100);
  socket().frame(Frame.OUTPUT, new Uint8Array(5));
  expect(output).toEqual([5]);
  socket().drop(1006);
  expect(statuses.at(-1)).toEqual({ state: "reconnecting" });
  await vi.advanceTimersByTimeAsync(500);
  expect(FakeSocket.all).toHaveLength(2);
  socket().open();
  const attach = decodeFrame(socket().sent[0]!).payload;
  expect(new TextDecoder().decode(attach.subarray(0, 43))).toBe("T".repeat(43));
  expect(new DataView(attach.buffer, attach.byteOffset + 43, 8).getBigUint64(0)).toBe(105n);
});

test("retries back off 0.5, 1, 2 s and give up after 60 s", async () => {
  const { socket, statuses } = start(() => Promise.reject(new Error("down")));
  socket().open();
  socket().drop(1006);
  await vi.advanceTimersByTimeAsync(500 + 1000 + 2000);
  expect(FakeSocket.all).toHaveLength(1);
  await vi.advanceTimersByTimeAsync(70_000);
  expect(statuses.at(-1)).toMatchObject({ state: "ended" });
});

test.each([[4000, "ended"], [4002, "ended"], [4003, "ended"], [4004, "ended"], [4410, "ended"], [4001, "taken"], [4403, "refused"]])(
  "close code %i is final and does not reconnect", async (code, state) => {
    const { socket, statuses } = start();
    socket().open();
    socket().drop(code);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(statuses.at(-1)).toMatchObject({ state });
  });

test("dispose leaves the shell running: the socket closes 1000 and no STOP goes out", () => {
  const { link, socket } = start();
  socket().open();
  link.dispose();
  expect(socket().closedWith).toBe(1000);
  expect(socket().sent.every((sent) => decodeFrame(sent).kind !== Frame.STOP)).toBe(true);
});

test("4004 says the machine disconnected", () => {
  const first = start();
  first.socket().open();
  first.socket().drop(4004);
  expect(first.statuses.at(-1)).toEqual({ state: "ended", note: "The machine disconnected." });
});

test("an attach that finds no terminal ends the retries at once", async () => {
  const { socket, statuses } = start(() => Promise.reject(new ApiError(404, "gone")));
  socket().open();
  socket().drop(1006);
  await vi.advanceTimersByTimeAsync(500);
  expect(statuses.at(-1)).toEqual({ state: "ended", note: "The terminal is gone." });
  await vi.advanceTimersByTimeAsync(30_000);
  expect(FakeSocket.all).toHaveLength(1);
});

test("a paste over 64 KiB goes out in frames of at most 64 KiB, in order, losing nothing", () => {
  const { link, socket } = start();
  socket().open();
  const paste = new Uint8Array(65_536 * 2 + 10).map((_, at) => at % 251);
  link.input(paste);
  const frames = socket().sent.slice(1).map((sent) => decodeFrame(sent));
  expect(frames.map((frame) => frame.payload.length)).toEqual([65_536, 65_536, 10]);
  expect(frames.every((frame) => frame.kind === Frame.INPUT)).toBe(true);
  const joined = new Uint8Array(paste.length);
  frames.reduce((at, frame) => { joined.set(frame.payload, at); return at + frame.payload.length; }, 0);
  expect(joined).toEqual(paste);
});

test("every ACK, first attach or reload, is followed by a RESIZE carrying the screen's size now", () => {
  const statuses: LinkStatus[] = [];
  let rows = 30;
  connectTerminal({ id: "t1", ticket: "A".repeat(43), onOutput: () => {}, onStatus: (status) => statuses.push(status),
    socket: (url) => new FakeSocket(url) as unknown as WebSocket, size: () => ({ rows, cols: 100 }) });
  const socket = FakeSocket.all.at(-1)!;
  socket.open();
  socket.ack(0);
  rows = 40;
  socket.ack(5);
  const resizes = socket.sent.slice(1).map((sent) => decodeFrame(sent)).filter((frame) => frame.kind === Frame.RESIZE);
  expect(resizes.map((frame) => [...frame.payload])).toEqual([[0, 30, 0, 100], [0, 40, 0, 100]]);
});

test("the lost-output marker before ACK is not counted: ACK sets the offset, later output adds to it", async () => {
  const { socket } = start();
  socket().open();
  socket().frame(Frame.OUTPUT, new TextEncoder().encode("\r\n--- output lost ---\r\n"));
  socket().ack(1000);
  socket().frame(Frame.OUTPUT, new Uint8Array(7));
  socket().drop(1006);
  await vi.advanceTimersByTimeAsync(500);
  socket().open();
  const attach = decodeFrame(socket().sent[0]!).payload;
  expect(new DataView(attach.buffer, attach.byteOffset + 43, 8).getBigUint64(0)).toBe(1007n);
});

function echoes() {
  const ms: number[] = [];
  const link = connectTerminal({
    id: "t1", ticket: "A".repeat(43), onOutput: () => {}, onStatus: () => {}, onEcho: (each) => ms.push(each),
    socket: (url) => new FakeSocket(url) as unknown as WebSocket, fresh: () => Promise.resolve("T".repeat(43)),
  });
  return { link, ms, socket: () => FakeSocket.all.at(-1)! };
}

test("the echo is the wait from the first unanswered INPUT to the next OUTPUT", () => {
  const { link, ms, socket } = echoes();
  socket().open();
  socket().ack(0);
  link.input(new Uint8Array([97]));
  vi.advanceTimersByTime(100);
  link.input(new Uint8Array([98]));
  vi.advanceTimersByTime(200);
  socket().frame(Frame.OUTPUT, new Uint8Array([97]));
  expect(ms).toEqual([300]);
  socket().frame(Frame.OUTPUT, new Uint8Array([98]));
  expect(ms).toEqual([300]);
});

test("output with no input waiting, and output replayed after an attach, is no echo", () => {
  const { link, ms, socket } = echoes();
  socket().open();
  socket().ack(0);
  socket().frame(Frame.OUTPUT, new Uint8Array([36]));
  link.input(new Uint8Array([97]));
  socket().ack(1);
  socket().frame(Frame.OUTPUT, new Uint8Array([97]));
  expect(ms).toEqual([]);
});

test("a dropped socket forgets the input it was waiting on", async () => {
  const { link, ms, socket } = echoes();
  socket().open();
  socket().ack(0);
  link.input(new Uint8Array([97]));
  socket().drop(1006);
  await vi.advanceTimersByTimeAsync(500);
  socket().open();
  socket().frame(Frame.OUTPUT, new Uint8Array([97]));
  expect(ms).toEqual([]);
});

class FakeChannel {
  binaryType = "blob";
  readyState = "open";
  sent: Uint8Array[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  send(data: Uint8Array) { this.sent.push(data); }
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
  readonly channel = new FakeChannel();
  iceGatheringState = "complete";
  localDescription = { sdp: "offer-sdp" };
  remote: { type: string; sdp: string } | null = null;
  closed = false;
  onicegatheringstatechange: (() => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  constructor() { FakePeer.all.push(this); }
  createDataChannel() { return this.channel; }
  createOffer() { return Promise.resolve({ type: "offer", sdp: "offer-sdp" }); }
  setLocalDescription() { return Promise.resolve(); }
  setRemoteDescription(description: { type: string; sdp: string }) { this.remote = description; return Promise.resolve(); }
  close() { this.closed = true; }
}

function linked() {
  FakePeer.all = [];
  const output: number[][] = [];
  const echoes: Array<[number, string]> = [];
  const paths: Array<[string, string | undefined]> = [];
  const link = connectTerminal({
    id: "t1", ticket: "A".repeat(43), iceServers: ["stun:s.test:3478"],
    onOutput: (bytes) => output.push([...bytes]), onStatus: () => {}, onEcho: (ms, path) => echoes.push([ms, path]),
    onPath: (path, note) => paths.push([path, note]),
    socket: (url) => new FakeSocket(url) as unknown as WebSocket, fresh: () => Promise.resolve("T".repeat(43)),
    peer: () => new FakePeer() as unknown as RTCPeerConnection,
  });
  const socket = () => FakeSocket.all.at(-1)!;
  const peer = () => FakePeer.all.at(-1)!;
  const kinds = () => socket().sent.map((sent) => decodeFrame(sent).kind);
  return { link, output, echoes, paths, socket, peer, kinds };
}

/** The relay is up, the offer is answered and the channel is open and fenced. */
async function direct(world: ReturnType<typeof linked>, offset = 0) {
  world.socket().open();
  world.socket().ack(offset);
  await vi.advanceTimersByTimeAsync(0);
  const answer = new TextEncoder().encode(JSON.stringify({ gen: 1, type: "answer", sdp: "answer-sdp" }));
  world.socket().frame(Frame.SIGNAL, answer);
  world.peer().channel.onopen?.();
  world.peer().channel.ack(offset);
  world.peer().channel.frame(Frame.ROUTE, Uint8Array.from([1, 0, 0, 0, 0, 0, 0, 0, 0]));
}

test("the first ACK of a socket offers a direct link over it, once, and the answer reaches the peer", async () => {
  const world = linked();
  world.socket().open();
  world.socket().ack(0);
  world.socket().ack(5);
  await vi.advanceTimersByTimeAsync(0);
  world.socket().frame(Frame.SIGNAL, new TextEncoder().encode(JSON.stringify({ gen: 1, type: "answer", sdp: "answer-sdp" })));

  expect(world.kinds().filter((kind) => kind === Frame.SIGNAL)).toHaveLength(1);
  expect(FakePeer.all).toHaveLength(1);
  expect(world.peer().remote).toEqual({ type: "answer", sdp: "answer-sdp" });
});

test("once the channel is up its output reaches the screen and the relay's does not; input follows the fence", async () => {
  const world = linked();
  await direct(world, 4);

  world.socket().frame(Frame.OUTPUT, new Uint8Array([1, 1]));
  world.peer().channel.frame(Frame.OUTPUT, new Uint8Array([2, 2, 2]));
  world.link.input(new Uint8Array([97]));
  world.link.resize(30, 100);
  world.link.stop();

  expect(world.output).toEqual([[2, 2, 2]]);
  const onChannel = world.peer().channel.sent.slice(1).map((sent) => decodeFrame(sent).kind);
  expect(onChannel).toEqual([Frame.INPUT, Frame.RESIZE]);
  const onRelay = world.kinds().slice(1);
  expect(onRelay).not.toContain(Frame.INPUT);
  expect(onRelay.at(-1)).toBe(Frame.STOP);
  expect(world.paths).toEqual([["direct", undefined]]);
});

test("a dropped channel hands output back to the relay, which counts from where the screen reached", async () => {
  const world = linked();
  await direct(world, 0);
  world.peer().channel.frame(Frame.OUTPUT, new Uint8Array(6));

  world.peer().channel.onclose?.();
  world.socket().ack(6);
  world.socket().frame(Frame.OUTPUT, new Uint8Array([7]));

  expect(world.output.at(-1)).toEqual([7]);
  // The host's ACK on the relay is not the socket's first: the retry waits its 5 s.
  expect(FakePeer.all).toHaveLength(1);
  expect(world.paths.at(-1)).toEqual(["relay", "Direct link lost; using the server."]);
  const route = decodeFrame(world.socket().sent.filter((sent) => decodeFrame(sent).kind === Frame.ROUTE).at(-1)!).payload;
  expect([...route.subarray(0, 1)]).toEqual([0]);
  expect(new DataView(route.buffer, route.byteOffset + 1, 8).getBigUint64(0)).toBe(6n);
});

test("a host that resumes on the relay past what the screen reached is marked as lost output", async () => {
  const world = linked();
  world.socket().open();
  world.socket().ack(0);
  world.socket().frame(Frame.OUTPUT, new Uint8Array(3));

  world.socket().ack(10);
  world.socket().frame(Frame.OUTPUT, new Uint8Array([1]));
  world.socket().ack(11);

  expect(world.output.map((bytes) => bytes.length)).toEqual([3, new TextEncoder().encode("\r\n--- output lost ---\r\n").length, 1]);
});

test("a lost socket drops the channel, and the next socket's ACK offers a new one", async () => {
  const world = linked();
  await direct(world, 0);

  world.socket().drop(1006);
  expect(world.peer().closed).toBe(true);
  expect(world.paths.at(-1)).toEqual(["relay", undefined]);
  await vi.advanceTimersByTimeAsync(500);
  world.socket().open();
  world.socket().ack(0);
  await vi.advanceTimersByTimeAsync(0);

  expect(FakePeer.all).toHaveLength(2);
});

test("the echo says which path carried it", async () => {
  const world = linked();
  world.socket().open();
  world.socket().ack(0);
  world.link.input(new Uint8Array([97]));
  vi.advanceTimersByTime(300);
  world.socket().frame(Frame.OUTPUT, new Uint8Array([97]));
  await vi.advanceTimersByTimeAsync(0);
  world.socket().frame(Frame.SIGNAL, new TextEncoder().encode(JSON.stringify({ gen: 1, type: "answer", sdp: "a" })));
  world.peer().channel.onopen?.();
  world.peer().channel.ack(1);
  world.peer().channel.frame(Frame.ROUTE, Uint8Array.from([1, 0, 0, 0, 0, 0, 0, 0, 0]));
  world.link.input(new Uint8Array([98]));
  vi.advanceTimersByTime(4);
  world.peer().channel.frame(Frame.OUTPUT, new Uint8Array([98]));

  expect(world.echoes).toEqual([[300, "relay"], [4, "direct"]]);
});

test("the shell's exit on the channel ends the link", async () => {
  const statuses: LinkStatus[] = [];
  FakePeer.all = [];
  connectTerminal({
    id: "t1", ticket: "A".repeat(43), onOutput: () => {}, onStatus: (status) => statuses.push(status),
    socket: (url) => new FakeSocket(url) as unknown as WebSocket, peer: () => new FakePeer() as unknown as RTCPeerConnection,
  });
  const socket = FakeSocket.all.at(-1)!;
  socket.open();
  socket.ack(0);
  await vi.advanceTimersByTimeAsync(0);
  FakePeer.all[0]!.channel.onopen?.();
  FakePeer.all[0]!.channel.frame(Frame.EXIT, Uint8Array.from([0, 0, 0, 2]));

  expect(statuses.at(-1)).toEqual({ state: "ended", note: "The shell exited.", exit: 2 });
});

test("opening a terminal sends only the fields it has, and a run adds its CLI and the chat session it re-opens", async () => {
  const session = "11111111-2222-4333-8444-555555555555";
  const sent = stubFetch(() => Response.json({ terminal_id: "t1", ticket: "k" }));
  await openTerminal("gx10", "w1", 24, 80, null);
  await openTerminal("gx10", "w1", 24, 80, null, { command: "claude", attach: session });
  expect(sent.map((each) => [each.path, each.body])).toEqual([
    ["/api/terminals", { machine: "gx10", workspace: "w1", rows: 24, cols: 80, surface: null }],
    ["/api/terminals", { machine: "gx10", workspace: "w1", rows: 24, cols: 80, surface: null, command: "claude", attach: session }],
  ]);
});

test("a run sends the folder it starts in, and a run without one sends none", async () => {
  const session = "11111111-2222-4333-8444-555555555555";
  const sent = stubFetch(() => Response.json({ terminal_id: "t1", ticket: "k" }));
  await openTerminal("gx10", "w1", 24, 80, null, { command: "codex", attach: session, cwd: "~/project" });
  await openTerminal("gx10", "w1", 24, 80, null, { command: "codex", attach: session });
  expect(sent.map((each) => each.body)).toEqual([
    { machine: "gx10", workspace: "w1", rows: 24, cols: 80, surface: null, command: "codex", attach: session, cwd: "~/project" },
    { machine: "gx10", workspace: "w1", rows: 24, cols: 80, surface: null, command: "codex", attach: session },
  ]);
});

test("the machines list carries each machine's installed CLIs", async () => {
  stubFetch(() => Response.json({ machines: [{ name: "gx10", status: "online", terminal_ready: true, confirmed: true, clis: ["claude", "git"] }] }));
  const listed = await listTerminalMachines("w1");
  expect(listed.machines[0]?.clis).toEqual(["claude", "git"]);
});
