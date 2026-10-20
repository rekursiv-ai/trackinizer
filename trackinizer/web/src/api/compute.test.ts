import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "./client";
import { listCompute } from "./compute";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

test("the compute read asks the server once and returns its notes in order", async () => {
  const notes = [{ name: "GPU jobs", about: "Campaigns run here." }, { name: "Shell", about: "Each chat's own box." }];
  const sent = stubFetch(() => Response.json({ notes }));
  expect(await listCompute()).toEqual(notes);
  expect(sent).toEqual([{ method: "GET", path: "/api/compute", query: "", headers: {}, body: undefined }]);
});

test("a server that notes nothing answers an empty list", async () => {
  stubFetch(() => Response.json({ notes: [] }));
  expect(await listCompute()).toEqual([]);
});

test("a refusal is the server's error", async () => {
  stubFetch(() => Response.json({ detail: "Not authenticated" }, { status: 401 }));
  await expect(listCompute()).rejects.toBeInstanceOf(ApiError);
});
