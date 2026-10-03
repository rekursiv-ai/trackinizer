/**
 * One write request: its body, frozen, and the idempotency key that goes with it.
 *
 * Make one per request, never one per user action. The server consumes a key on
 * the request's first change, so a second request under the same key is replayed
 * or refused, and it does not compare bodies: a retry under the same key with a
 * changed body replays the original and silently drops the change. To retry a
 * request, send the same `Keyed` again; that is the only reuse.
 */
export type Keyed<Body> = { readonly key: string; readonly body: Body };

/**
 * Pair `body` with a fresh idempotency key, kept as the JSON that is sent and
 * deeply frozen. A copy that kept a `Date` would not do: a frozen `Date` still
 * runs its setters, so the text sent could change after keying.
 */
export function keyed<Body>(body: Body): Keyed<Body> {
  return Object.freeze({ key: newUuid(), body: deepFreeze(JSON.parse(JSON.stringify(body)) as Body) });
}

/**
 * A fresh random UUID (version 4): an idempotency key, a request's id, a new
 * saved view's id.
 *
 * `crypto.randomUUID` exists only in a secure context (HTTPS or localhost). The
 * app opened over plain HTTP from another machine lacks it, and calling it there
 * failed every request (parity bug B1), so this builds one from
 * `crypto.getRandomValues`, which every context has, as the old UI's `uuidv4` did.
 */
export function newUuid(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function deepFreeze<T>(value: T): T {
  if (typeof value === "object" && value !== null) {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}
