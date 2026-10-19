import { expect, test } from "vitest";
import { decodeFrame, decodeOffset, decodeRoute, encodeAttach, encodeFrame, encodeRoute, encodeWinsize, Frame, Route } from "./terminalFrames";

test("every kind round-trips, with an empty payload and a 64 KiB one", () => {
  for (const kind of Object.values(Frame)) {
    for (const payload of [new Uint8Array(), new Uint8Array(65_536).fill(7)]) {
      const back = decodeFrame(encodeFrame(kind, payload));
      expect(back.kind).toBe(kind);
      expect(back.payload).toEqual(payload);
    }
  }
});

test("a short header, a disagreeing length and an unknown kind are refused", () => {
  expect(() => decodeFrame(new Uint8Array([2, 0]))).toThrow("shorter");
  const frame = encodeFrame(Frame.INPUT, new Uint8Array([1, 2]));
  expect(() => decodeFrame(frame.subarray(0, 6))).toThrow("disagrees");
  expect(() => decodeFrame(new Uint8Array([99, 0, 0, 0, 0]))).toThrow("unknown");
});

test("the resize payload is rows then columns, two bytes each", () => {
  expect([...encodeWinsize(24, 300)]).toEqual([0, 24, 1, 44]);
});

test("the attach payload is the ticket then a big-endian u64 offset, past 2^32", () => {
  const ticket = "t".repeat(43);
  const payload = encodeAttach(ticket, 2 ** 33 + 5);
  expect(payload.length).toBe(51);
  expect(new TextDecoder().decode(payload.subarray(0, 43))).toBe(ticket);
  expect(decodeOffset(payload.subarray(43))).toBe(2 ** 33 + 5);
  expect(() => decodeOffset(new Uint8Array(3))).toThrow("eight");
});

test("the link kinds equal the server's: SIGNAL 9, ROUTE 10, KEEPALIVE 11", () => {
  expect([Frame.SIGNAL, Frame.ROUTE, Frame.KEEPALIVE]).toEqual([9, 10, 11]);
});

test("the route payload is a route byte then a big-endian u64, and a malformed one is refused", () => {
  const payload = encodeRoute(Route.DIRECT, 2 ** 33 + 1);
  expect([...payload.subarray(0, 1)]).toEqual([1]);
  expect(decodeRoute(payload)).toEqual({ route: Route.DIRECT, offset: 2 ** 33 + 1 });
  expect(() => decodeRoute(payload.subarray(1))).toThrow("route");
  expect(() => decodeRoute(new Uint8Array([2, 0, 0, 0, 0, 0, 0, 0, 0]))).toThrow("route");
});
