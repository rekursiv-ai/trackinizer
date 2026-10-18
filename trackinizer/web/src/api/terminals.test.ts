import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Frame, decodeFrame, encodeFrame } from "../visuals/terminalFrames";
import { ApiError } from "./client";
import { connectTerminal, type LinkStatus } from "./terminals";

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

test.each([[4000, "ended"], [4002, "ended"], [4003, "ended"], [4004, "ended"], [4410, "ended"], [4001, "taken"], [4403, "refused"], [4429, "refused"]])(
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

test("4004 says the machine disconnected, and 4429 says the limit was reached", () => {
  const first = start();
  first.socket().open();
  first.socket().drop(4004);
  expect(first.statuses.at(-1)).toEqual({ state: "ended", note: "The machine disconnected." });
  const second = start();
  second.socket().open();
  second.socket().drop(4429);
  expect(second.statuses.at(-1)).toEqual({ state: "refused", note: "Terminal limit reached; close one." });
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
