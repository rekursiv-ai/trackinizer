import { afterEach, expect, test, vi } from "vitest";
import { getCodeDiff, readCodeFile } from "./code";
import { stubFetch } from "./testing";

afterEach(() => {
  vi.unstubAllGlobals();
});

const FROM = "00000000-0000-4000-8000-000000000001";
const TO = "00000000-0000-4000-8000-000000000002";
const SHA = "a".repeat(64);

test("a diff is one GET naming its two records, earlier side first, with no body", async () => {
  const answer = { files: [{ path: "x.py", change: "modified", old_sha256: SHA, new_sha256: SHA, diff: "--- a/x.py\n+++ b/x.py\n" }], more: false };
  const sent = stubFetch(() => Response.json(answer));
  expect(await getCodeDiff(FROM, TO)).toEqual(answer);
  expect(sent).toEqual([{ method: "GET", path: "/api/code/diff", query: `?from=${FROM}&to=${TO}`, headers: {}, body: undefined }]);
});

test("a diff either of whose records has no snapshot the reader may read is null, and any other failure throws", async () => {
  stubFetch(() => Response.json({ detail: "No code snapshot for a record." }, { status: 404 }));
  expect(await getCodeDiff(FROM, TO)).toBeNull();
  stubFetch(() => Response.json({ detail: "store down" }, { status: 503 }));
  await expect(getCodeDiff(FROM, TO)).rejects.toMatchObject({ status: 503, detail: "store down" });
  stubFetch(() => Response.json({ files: "none" }));
  await expect(getCodeDiff(FROM, TO)).rejects.toThrow("Invalid code diff");
});

test("one file is a GET of the record's file by its path, which the query carries encoded", async () => {
  const sent = stubFetch(() => Response.json({ path: "src/a b.py", sha256: SHA, text: "x = 1\n" }));
  expect(await readCodeFile(TO, "src/a b.py")).toEqual({ path: "src/a b.py", sha256: SHA, text: "x = 1\n" });
  expect(sent.map(({ method, path, query }) => [method, path, new URLSearchParams(query).get("path")])).toEqual([["GET", `/api/code/records/${TO}/file`, "src/a b.py"]]);
  stubFetch(() => Response.json({ detail: "No such file in a code snapshot." }, { status: 404 }));
  await expect(readCodeFile(TO, "gone.py")).rejects.toMatchObject({ status: 404 });
  stubFetch(() => Response.json({ path: "x.py", sha256: SHA }));
  await expect(readCodeFile(TO, "x.py")).rejects.toThrow("Invalid code file");
});
