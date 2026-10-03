import { afterEach, expect, test, vi } from "vitest";
import { keyed } from "./idempotency";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("keys come without crypto.randomUUID, which a page over plain HTTP lacks (B1)", () => {
  const real = globalThis.crypto;
  vi.stubGlobal("crypto", { getRandomValues: real.getRandomValues.bind(real) });
  const [first, second] = [keyed({}).key, keyed({}).key];
  expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  expect(second).not.toBe(first);
});

test("every request gets a fresh key", () => {
  const body = { value: "x" };
  expect(keyed(body).key).not.toBe(keyed(body).key);
  expect(keyed(body).key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test("the body is frozen, and later edits to the original do not reach it", () => {
  const body = { title: "Ship it", labels: ["webui"] };
  const write = keyed(body);
  body.labels.push("later");
  expect(write.body).toEqual({ title: "Ship it", labels: ["webui"] });
  expect(Object.isFrozen(write)).toBe(true);
  expect(Object.isFrozen(write.body)).toBe(true);
  expect(Object.isFrozen(write.body.labels)).toBe(true);
});

test("the body is kept as the JSON it is sent as, so nothing reached through it can change it (WEB-17)", () => {
  // A frozen Date still runs its setters: freezing a copy of one would not fix the text sent.
  const write = keyed({ started: new Date("2026-09-27T12:00:00Z") } as { started: unknown });
  if (write.body.started instanceof Date) write.body.started.setUTCFullYear(2000);
  expect(write.body).toEqual({ started: "2026-09-27T12:00:00.000Z" });
});
