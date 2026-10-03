import { expect, test } from "vitest";
import { CHECK_IDS, idFilter, isPageOf, newerRow, type Page, placeRow, seqRanges, serverOrder } from "./rows";
import { issue, mount, testClient, uuid } from "./testing";

test("seqs become the fewest inclusive ranges", () => {
  expect(seqRanges([9, 3, 4, 5, 4, 12, 11])).toEqual(["3..5", "9..9", "11..12"]);
  expect(seqRanges([7])).toEqual(["7..7"]);
  expect(seqRanges([])).toEqual([]);
});

test("a membership check names 63 ids by their last seven hex digits, within the server's 512 characters, and no more", () => {
  const ids = Array.from({ length: CHECK_IDS + 1 }, (_, n) => uuid(n));
  const filter = idFilter(ids.slice(0, CHECK_IDS));
  expect(filter).toMatchObject({ field: "id", op: "re" });
  expect(filter.value).toBe(`(${ids.slice(0, CHECK_IDS).map((id) => id.slice(-7)).join("|")})$`);
  expect(filter.value.length).toBe(506);
  expect(new RegExp(filter.value).test(ids[5]!)).toBe(true);
  expect(new RegExp(filter.value).test(ids[CHECK_IDS]!)).toBe(false);
  // 64 would make 514 characters, which the server refuses.
  expect(`(${ids.map((id) => id.slice(-7)).join("|")})$`.length).toBe(514);
  expect(() => idFilter(ids)).toThrow("at most 63 ids");
});

test("rows sort as the server lists them: newest created first, then the larger id", () => {
  const older = issue(1);
  const newer = issue(2);
  const twin = { ...issue(3), created: newer.created };
  expect([older, newer, twin].toSorted(serverOrder).map((row) => row.seq)).toEqual([3, 2, 1]);
});

test("rows created within one millisecond sort by their microseconds, as the server's order does", () => {
  // The larger id is the older row, so an order that saw only milliseconds would put it first.
  const older = { ...issue(9), created: "2026-09-20T00:00:00.000100+00:00" };
  const newer = { ...issue(1), created: "2026-09-20T00:00:00.000900+00:00" };
  const whole = { ...issue(5), created: "2026-09-20T00:00:00+00:00" };
  expect([older, whole, newer].toSorted(serverOrder).map((row) => row.seq)).toEqual([1, 9, 5]);
});

test("a list's pages are those with exactly its filters, not a longer list of them", () => {
  const client = testClient();
  const active = [{ field: "status", op: "is", value: "active" }] as const;
  mount(client, ["inquiries", "list", active, "Issue", 50, 0], []);
  mount(client, ["inquiries", "list", [...active, { field: "owner", op: "is", value: "dan" }], "Issue", 50, 0], []);
  mount(client, ["detail", uuid(1)], {});
  const pages = client.getQueryCache().findAll({ predicate: (query) => isPageOf(query, active) });
  expect(pages.map((query) => query.queryKey[2])).toEqual([active]);
});

test("a row that entered goes where the server would list it, or nowhere when beyond the loaded rows", () => {
  const page = (offset: number, seqs: number[], pageSize = 3): Page => ({
    key: ["p", offset],
    kind: "Issue",
    pageSize,
    offset,
    rows: seqs.map((seq) => issue(seq)),
    fetching: false,
  });
  const full = [page(0, [9, 8, 7]), page(3, [6, 5, 4])];
  expect(placeRow(full, issue(10))).toEqual({ page: full[0], index: 0 });
  expect(placeRow(full, issue(5.5))).toEqual({ page: full[1], index: 1 });
  // Older than every loaded row while the last page is full: more rows lie beyond.
  expect(placeRow(full, issue(2))).toBeNull();
  // The last page is short, so the server has nothing older: it goes at the end.
  const short = [page(0, [9, 8, 7]), page(3, [6])];
  expect(placeRow(short, issue(2))).toEqual({ page: short[1], index: 1 });
  // An empty list takes any row.
  const empty = [page(0, [])];
  expect(placeRow(empty, issue(1))).toEqual({ page: empty[0], index: 0 });
  // A page still loading takes nothing yet.
  expect(placeRow([{ ...page(0, []), rows: undefined }], issue(1))).toBeNull();
});

test("a row read afresh replaces the one held, unless the one held is a later version, to the microsecond", () => {
  const held = issue(1, { modified: "2026-09-21T00:00:00.000002+00:00" });
  expect(newerRow(issue(1, { modified: "2026-09-21T00:00:00.000001+00:00" }), held)).toBe(held);
  const later = issue(1, { modified: "2026-09-21T00:00:00.000003+00:00" });
  expect(newerRow(later, held)).toBe(later);
});
