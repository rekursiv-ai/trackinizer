import { afterEach, expect, test, vi } from "vitest";
import type { SessionRecord } from "../api/sessions";
import { stubFetch } from "../api/testing";
import { readChatParts } from "./chatRecords";

afterEach(() => vi.unstubAllGlobals());

const record = (idx: number): SessionRecord => ({ idx, kind: "AssistantMessage", payload: { content: `r${idx}` }, text: "" }) as unknown as SessionRecord;

/** A server holding `parts` (records per part, as their counts), which answers parts and records. */
function server(parts: { [part: number]: number }) {
  return stubFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname.endsWith("/parts")) {
      return Response.json({ parts: Object.entries(parts).map(([part, records]) => ({ part: Number(part), name: `p${part}`, format: "sagent", records, metadata: {}, ir_id: "i" })) });
    }
    const part = Number(url.searchParams.get("part"));
    const after = Number(url.searchParams.get("after_idx"));
    const limit = Number(url.searchParams.get("limit"));
    const idxs = Array.from({ length: parts[part] ?? 0 }, (_, n) => n).filter((n) => n > after).slice(0, limit);
    return Response.json({ part, records: idxs.map(record) });
  });
}

test("every part the session lists is read in part order, and the legacy backfill is left alone", async () => {
  const sent = server({ [-1]: 3, 0: 2, 1: 1 });
  const read = await readChatParts("s1", undefined);
  expect(read.map(({ part, records }) => [part, records.map((each) => each.idx)])).toEqual([[0, [0, 1]], [1, [0]]]);
  expect(sent.filter((each) => each.path.endsWith("/records")).map((each) => each.query)).toEqual([
    "?part=0&after_idx=-1&limit=1000&plaintext_only=true", "?part=1&after_idx=-1&limit=1000&plaintext_only=true",
  ]);
});

test("a later read asks only for the records after those held, and none for a part that gained nothing", async () => {
  const sent = server({ 0: 3, 1: 2 });
  const held = [{ part: 0, records: [record(0), record(1)] }, { part: 1, records: [record(0), record(1)] }];
  const read = await readChatParts("s1", held);
  expect(read[0]!.records.map((each) => each.idx)).toEqual([0, 1, 2]);
  expect(read[1]!.records).toEqual(held[1]!.records);
  expect(sent.filter((each) => each.path.endsWith("/records")).map((each) => each.query)).toEqual([
    "?part=0&after_idx=1&limit=1000&plaintext_only=true",
  ]);
});

test("a listing that counts records not yet written ends the read at what the server has", async () => {
  server({ 0: 2 });
  stubFetch((request) => {
    const url = new URL(request.url);
    return url.pathname.endsWith("/parts")
      ? Response.json({ parts: [{ part: 0, name: "p", format: "sagent", records: 5, metadata: {}, ir_id: "i" }] })
      : Response.json({ part: 0, records: [record(0), record(1)] });
  });
  const read = await readChatParts("s1", undefined);
  expect(read[0]!.records).toHaveLength(2);
});

test("a long part is read in pages of a thousand", async () => {
  const sent = server({ 0: 2_500 });
  const read = await readChatParts("s1", undefined);
  expect(read[0]!.records).toHaveLength(2_500);
  expect(sent.filter((each) => each.path.endsWith("/records"))).toHaveLength(3);
});
