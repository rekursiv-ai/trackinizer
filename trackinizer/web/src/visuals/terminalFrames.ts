/**
 * The terminal socket's frames: one byte of kind, a four-byte payload length, the
 * payload. Every integer is unsigned and big-endian. One WebSocket binary message
 * is one frame.
 */

export const Frame = { ATTACH: 1, INPUT: 2, RESIZE: 3, STOP: 5, OUTPUT: 6, ACK: 7, EXIT: 8, SIGNAL: 9, ROUTE: 10, KEEPALIVE: 11 } as const;
export type FrameKind = (typeof Frame)[keyof typeof Frame];

/** One decoded frame. */
export type Decoded = { readonly kind: FrameKind; readonly payload: Uint8Array };

const HEADER = 5;
const KINDS: ReadonlySet<number> = new Set(Object.values(Frame));

/** Frame `payload` as one `kind` record. */
export function encodeFrame(kind: FrameKind, payload: Uint8Array = new Uint8Array()): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(HEADER + payload.length);
  bytes[0] = kind;
  new DataView(bytes.buffer).setUint32(1, payload.length);
  bytes.set(payload, HEADER);
  return bytes;
}

/** The frame in one whole message; a short or disagreeing message or an unknown kind is an error. */
export function decodeFrame(message: Uint8Array): Decoded {
  if (message.length < HEADER) throw new Error("terminal frame is shorter than its header");
  const length = new DataView(message.buffer, message.byteOffset, message.byteLength).getUint32(1);
  if (message.length !== HEADER + length) throw new Error("terminal frame length disagrees with its message");
  const kind = message[0]!;
  if (!KINDS.has(kind)) throw new Error(`unknown terminal frame kind ${kind}`);
  return { kind: kind as FrameKind, payload: message.subarray(HEADER) };
}

/** The `RESIZE` payload: rows then columns, two bytes each. */
export function encodeWinsize(rows: number, cols: number): Uint8Array {
  const bytes = new Uint8Array(4);
  const view = new DataView(bytes.buffer);
  view.setUint16(0, rows);
  view.setUint16(2, cols);
  return bytes;
}

/** The `ATTACH` payload: the 43-character ticket, then the u64 offset the stream resumes at. */
export function encodeAttach(ticket: string, offset: number): Uint8Array {
  const text = new TextEncoder().encode(ticket);
  const bytes = new Uint8Array(text.length + 8);
  bytes.set(text);
  new DataView(bytes.buffer).setBigUint64(text.length, BigInt(offset));
  return bytes;
}

/** The u64 an `ACK` carries. */
export function decodeOffset(payload: Uint8Array): number {
  if (payload.length !== 8) throw new Error("terminal offset is not eight bytes");
  return Number(new DataView(payload.buffer, payload.byteOffset, 8).getBigUint64(0));
}

/** Where output travels: the server's relay, or the direct data channel. */
export const Route = { RELAY: 0, DIRECT: 1 } as const;
export type RouteKind = (typeof Route)[keyof typeof Route];

/** The `ROUTE` payload: a route byte, then the u64 stream offset the sender has reached. */
export function encodeRoute(route: RouteKind, offset: number): Uint8Array {
  const bytes = new Uint8Array(9);
  bytes[0] = route;
  new DataView(bytes.buffer).setBigUint64(1, BigInt(offset));
  return bytes;
}

/** The route and offset a `ROUTE` payload carries; anything else is an error. */
export function decodeRoute(payload: Uint8Array): { readonly route: RouteKind; readonly offset: number } {
  if (payload.length !== 9 || (payload[0] !== Route.RELAY && payload[0] !== Route.DIRECT)) throw new Error("terminal route payload is malformed");
  return { route: payload[0], offset: Number(new DataView(payload.buffer, payload.byteOffset + 1, 8).getBigUint64(0)) };
}
