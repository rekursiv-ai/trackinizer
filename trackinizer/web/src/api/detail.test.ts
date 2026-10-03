import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "./client";
import { findRef, getDetail, getEvidenceConfidence } from "./detail";
import { stubFetch } from "./testing";

const ID = "0b6f7c1e-2f7a-4c55-9d7e-1f0e6b1d2a33";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("each detail read is a bodiless GET of its route", async () => {
  const detail = { self: { id: ID, kind: "Issue", seq: 7 }, edges: {}, backlinks: {}, changes: [] };
  const bodies: { [path: string]: unknown } = {
    [`/api/web/get/${ID}`]: detail,
    "/api/inquiries/Issue/7": { id: ID, kind: "Issue", seq: 7, narrows: [] },
    [`/api/inquiries/${ID}/confidence`]: { confidence: 0.73 },
  };
  const sent = stubFetch((request) => Response.json(bodies[new URL(request.url).pathname]));
  expect(await getDetail(ID)).toEqual(detail);
  expect(await findRef("Issue", 7)).toBe(ID);
  expect(await getEvidenceConfidence(ID)).toBe(0.73);
  expect(sent).toEqual(
    Object.keys(bodies).map((path) => ({ method: "GET", path, query: "", headers: {}, body: undefined })),
  );
});

test("a missing row is an ApiError carrying the server's 404 message", async () => {
  stubFetch(() => Response.json({ detail: "Issue#9 not found" }, { status: 404 }));
  const error = await findRef("Issue", 9).catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 404, detail: "Issue#9 not found" });
});

test("a Kind#seq of a kind this build does not know fails before any request (R2-X3)", async () => {
  const sent = stubFetch(() => Response.json({ id: ID }));
  await expect(findRef("Ticket", 9)).rejects.toThrow("This build does not know the inquiry kind Ticket");
  expect(sent).toEqual([]);
});
