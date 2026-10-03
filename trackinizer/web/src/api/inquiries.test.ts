import { afterEach, expect, test, vi } from "vitest";
import { keyed } from "./idempotency";
import {
  type Ancestor,
  type CreateBody,
  clearField,
  createInquiry,
  INQUIRY_ROW_FIELDS,
  type InquiryRow,
  inquiryKinds,
  isInquiryKind,
  listInquiries,
  listInquiriesBySeq,
  type SetFieldBody,
  setField,
} from "./inquiries";
import { stubFetch } from "./testing";

const ID = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("a field write is a PUT with its key in the header", async () => {
  const sent = stubFetch(() => Response.json({ id: ID, change_id: "c1" }));
  const write = keyed<SetFieldBody<"/api/inquiries/{target_id}/status">>({
    value: "complete",
    mode: "cas",
    expected: "active",
  });
  expect(await setField("/api/inquiries/{target_id}/status", ID, write)).toEqual({
    id: ID,
    change_id: "c1",
  });
  expect(sent).toEqual([
    {
      method: "PUT",
      path: `/api/inquiries/${ID}/status`,
      query: "",
      headers: { "content-type": "application/json", "idempotency-key": write.key },
      body: { value: "complete", mode: "cas", expected: "active" },
    },
  ]);
});

test("clearing a field is a DELETE with a {} body and its key in the header", async () => {
  const sent = stubFetch(() => Response.json({ id: ID, change_id: null }));
  const write = keyed({});
  await clearField("/api/issue/{target_id}/priority", ID, write);
  expect(sent).toEqual([
    {
      method: "DELETE",
      path: `/api/issue/${ID}/priority`,
      query: "",
      headers: { "content-type": "application/json", "idempotency-key": write.key },
      body: {},
    },
  ]);
});

test("a create is a POST to the kind in lowercase, with its key in the body", async () => {
  const sent = stubFetch(() => Response.json({ id: ID }, { status: 201 }));
  const write = keyed<CreateBody<"Issue">>({ title: "Ship it", narrows: [[ID, 10]] });
  expect(await createInquiry("Issue", write)).toEqual({ id: ID });
  expect(sent).toEqual([
    {
      method: "POST",
      path: "/api/inquiries/issue",
      query: "",
      headers: { "content-type": "application/json" },
      body: { title: "Ship it", narrows: [[ID, 10]], idempotency_key: write.key },
    },
  ]);
});

test("list kinds are checked against the schema this build is typed against", () => {
  expect(inquiryKinds(["Issue", "CodeChange"])).toEqual(["Issue", "CodeChange"]);
  expect(isInquiryKind("AgentSession")).toBe(true);
  // Kinds are PascalCase; an inherited property is not a kind.
  for (const kind of ["issue", "Ticket", "toString"]) expect(isInquiryKind(kind), kind).toBe(false);
  expect(() => inquiryKinds(["Issue", "Ticket"])).toThrow("This build does not know the inquiry kind Ticket");
});

test("a retry sends the same key and body again", async () => {
  const sent = stubFetch();
  const write = keyed({ value: "Renamed" });
  await setField("/api/inquiries/{target_id}/title", ID, write);
  await setField("/api/inquiries/{target_id}/title", ID, write);
  expect(sent[1]).toEqual(sent[0]);
});

test("a list page repeats kind and filter, with limit and offset per kind", async () => {
  const sent = stubFetch(() => Response.json([{ id: ID, kind: "Issue", seq: 3 }]));
  const filters = [
    { field: "status", op: "ne", value: "active" },
    { field: "owner", op: "re", value: "^(dan@example\\.com|josh)$" },
  ];
  const rows = await listInquiries({ kinds: ["Issue", "Belief"], filters, limit: 20, offset: 40 });
  expect(rows).toEqual([{ id: ID, kind: "Issue", seq: 3 }]);
  expect(sent).toHaveLength(1);
  expect(sent[0]).toMatchObject({ method: "GET", path: "/api/inquiries", body: undefined });
  const query = new URLSearchParams(sent[0]!.query);
  expect(query.getAll("kind")).toEqual(["Issue", "Belief"]);
  expect(query.getAll("filter").map((raw) => JSON.parse(raw))).toEqual(filters);
  expect([query.get("limit"), query.get("offset")]).toEqual(["20", "40"]);
  // Without `fields` the server sends every key; without `ancestors`, none.
  expect(query.has("fields")).toBe(false);
  expect(query.has("ancestors")).toBe(false);
});

test("a list page can ask for each row's narrows ancestors, and a list filter on narrows", async () => {
  const ancestor = { id: ID, kind: "Issue", seq: 1, title: "Goal", status: "active", child_ids: [ID] };
  const sent = stubFetch(() => Response.json([{ id: ID, ancestors: [ancestor] }]));
  const roots = [{ field: "narrows", op: "notnull", value: "" }];
  const rows = await listInquiries({
    kinds: ["Issue"],
    filters: roots,
    limit: 50,
    offset: 0,
    fields: ["id"],
    ancestors: "narrows",
  });
  const ancestry: readonly Ancestor[] | undefined = rows[0]!.ancestors;
  expect(ancestry).toEqual([ancestor]);
  const query = new URLSearchParams(sent[0]!.query);
  expect(query.get("ancestors")).toBe("narrows");
  expect(query.getAll("filter").map((raw) => JSON.parse(raw))).toEqual(roots);
});

test("a list page can name the only keys its rows carry, one fields param each", async () => {
  const sent = stubFetch(() => Response.json([{ id: ID, title: "Retry" }]));
  const rows = await listInquiries({ kinds: ["Issue"], filters: [], limit: 50, offset: 0, fields: ["id", "title"] });
  expect(rows).toEqual([{ id: ID, title: "Retry" }]);
  expect(new URLSearchParams(sent[0]!.query).getAll("fields")).toEqual(["id", "title"]);
  // A row is typed with the keys asked for alone,
  // @ts-expect-error
  expect(rows[0]!.status).toBeUndefined();
  // and a page names no key a list row lacks.
  // @ts-expect-error
  await listInquiries({ kinds: ["Issue"], filters: [], limit: 50, offset: 0, fields: ["description"] });
});

test("INQUIRY_ROW_FIELDS asks for every key of a list row", async () => {
  const sent = stubFetch(() => Response.json([]));
  // The rows type as whole list rows: no key of `InquiryRow` is missing.
  const rows: InquiryRow[] = await listInquiries({
    kinds: ["Belief"],
    filters: [],
    limit: 50,
    offset: 0,
    fields: INQUIRY_ROW_FIELDS,
  });
  expect(rows).toEqual([]);
  const asked = new URLSearchParams(sent[0]!.query).getAll("fields");
  expect(asked).toEqual([...INQUIRY_ROW_FIELDS]);
  expect(asked).toEqual(expect.arrayContaining(["id", "kind", "seq", "title", "proved_by", "favored_by"]));
  expect(asked).not.toContain("description");
});

test("a seq-range read repeats seq_range with the kinds and filters, and a limit per kind", async () => {
  const sent = stubFetch(() => Response.json([{ id: ID, kind: "Issue", seq: 3 }]));
  const filters = [{ field: "status", op: "is", value: "active" }];
  await listInquiriesBySeq({ kinds: ["Issue", "Belief"], filters, seqRanges: ["3..5", "9..9"], limit: 4 });
  const query = new URLSearchParams(sent[0]!.query);
  expect(sent[0]).toMatchObject({ method: "GET", path: "/api/inquiries", body: undefined });
  expect(query.getAll("kind")).toEqual(["Issue", "Belief"]);
  expect(query.getAll("filter").map((raw) => JSON.parse(raw))).toEqual(filters);
  expect(query.getAll("seq_range")).toEqual(["3..5", "9..9"]);
  expect([query.get("limit"), query.get("offset")]).toEqual(["4", null]);
  expect(query.has("fields")).toBe(false);
});

test("a seq-range read names its fields as a list page does", async () => {
  const sent = stubFetch(() => Response.json([{ id: ID, seq: 3 }]));
  const rows = await listInquiriesBySeq({ kinds: ["Issue"], filters: [], seqRanges: ["3..3"], limit: 1, fields: ["id", "seq"] });
  expect(rows).toEqual([{ id: ID, seq: 3 }]);
  expect(new URLSearchParams(sent[0]!.query).getAll("fields")).toEqual(["id", "seq"]);
  // @ts-expect-error
  expect(rows[0]!.title).toBeUndefined();
});

test("a seq-range read asks for ancestors as a list page does", async () => {
  const sent = stubFetch(() => Response.json([]));
  await listInquiriesBySeq({ kinds: ["Issue"], filters: [], seqRanges: ["3..3"], limit: 1, ancestors: "narrows" });
  expect(new URLSearchParams(sent[0]!.query).get("ancestors")).toBe("narrows");
});
