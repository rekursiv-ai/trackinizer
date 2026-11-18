import { afterEach, expect, test, vi } from "vitest";
import { filedRows, loadCampaignSummary, readTrust, trustedRows } from "./summaries";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

const step = { start: 0, end: 1, title: "Fix", text: "Fixed.", learned: "" };
const session = (id: string) => ({
  kind: "session", version: 1, session: id, ref: `AgentSession#${id}`, state: "running", as_of: "2026-10-12T00:00:00+00:00",
  title: `Session ${id}`, ask: "a", overview: "o", outcome: "success", items: 2, tool_calls: 0, errors: 0,
  learnings: 0, started: "", steps: [step], phases: [step], beliefs: [], anchors: [],
});
const corpus = {
  kind: "campaign", version: 1, campaign: "camp", issue: "Issue#5", state: "running", as_of: "2026-10-12T00:00:00+00:00",
  sessions: [], efforts: [], claims: [],
};
const ME = "campaign@service.invalid";
const row = (document: object | string, labels: string[], account = ME) => ({
  id: "x", kind: "Artifact", seq: 1, title: "t", status: "complete", labels, account,
  description: typeof document === "string" ? document : JSON.stringify(document),
});

function labelOf(request: Request): string {
  const filter = new URL(request.url).searchParams.get("filter");
  return (JSON.parse(filter ?? "{}") as { value: string }).value;
}

test("a brief Issue's rows are read by its label, the campaign row apart from the session rows", async () => {
  const sent = stubFetch((request) => Response.json(
    labelOf(request) === "summary:issue:ISSUE"
      ? [row(corpus, ["summary:campaign:lead"]), row(session("1"), ["summary:session:1"]), row(session("2"), [])]
      : [],
  ));

  const summary = await loadCampaignSummary("ISSUE", [ME]);

  expect(summary?.corpus?.campaign).toBe("camp");
  expect([...summary!.sessions.keys()]).toEqual(["1", "2"]);
  expect(sent.map((s) => [s.method, s.path])).toEqual([["GET", "/api/inquiries"]]);
  const query = new URL(`http://x${sent[0]!.query}`).searchParams;
  expect(query.getAll("kind")).toEqual(["Artifact"]);
  expect(JSON.parse(query.get("filter")!)).toEqual({ field: "labels", op: "is", value: "summary:issue:ISSUE" });
});

test("a session's own row leads to its campaign by the lead label", async () => {
  const sent = stubFetch((request) => {
    const label = labelOf(request);
    if (label === "summary:session:S1") return Response.json([row(session("1"), ["summary:session:S1", "summary:lead:L1"])]);
    if (label === "summary:lead:L1") return Response.json([row(corpus, []), row(session("1"), []), row(session("2"), [])]);
    return Response.json([]);
  });

  const summary = await loadCampaignSummary("S1", [ME]);

  expect(sent.map((s) => labelOf(new Request(`http://x${s.path}${s.query}`)))).toEqual([
    "summary:issue:S1", "summary:session:S1", "summary:lead:L1",
  ]);
  expect(summary?.corpus).not.toBeNull();
  expect(summary?.sessions.size).toBe(2);
});

test("a record with no summary answers null", async () => {
  stubFetch(() => Response.json([]));

  expect(await loadCampaignSummary("NONE", [ME])).toBeNull();
});

test("a row this build cannot read is left out", async () => {
  stubFetch(() => Response.json([row("not json", []), row({ ...session("1"), version: 2 }, []), row(session("3"), [])]));

  const summary = await loadCampaignSummary("ISSUE", [ME]);

  expect([...summary!.sessions.keys()]).toEqual(["3"]);
});

test("a row filed by anyone but a trusted account is left out, even with the right labels", async () => {
  stubFetch(() => Response.json([
    row(session("1"), ["summary:session:1"], "mallory@example.com"),
    row(session("2"), []),
    row(corpus, [], "mallory@example.com"),
  ]));

  const summary = await loadCampaignSummary("ISSUE", [ME]);

  expect([...summary!.sessions.keys()]).toEqual(["2"]);
  expect(summary!.corpus).toBeNull();
});

test("with no trusted account nothing is read", async () => {
  stubFetch(() => Response.json([row(session("1"), [])]));

  expect(await loadCampaignSummary("ISSUE", [])).toBeNull();
});

test("the audiences and the trusted accounts come from the profile", async () => {
  stubFetch(() => Response.json({ read_audiences: ["open", "staff"], summary_accounts: [ME] }));

  expect(await readTrust()).toEqual({ audiences: ["open", "staff"], accounts: [ME] });
});

test("a profile with neither reads none", async () => {
  stubFetch(() => Response.json({}));

  expect(await readTrust()).toEqual({ audiences: [], accounts: [] });
});

test("trusted rows carry every label asked for, each its own filter, keep only a trusted account's, and say who filed each and when it changed", async () => {
  const sent = stubFetch(() => Response.json([
    { ...row(corpus, ["a", "b"]), modified: "2026-10-12T09:00:00+00:00" },
    row(corpus, ["a", "b"], "mallory@example.com"),
  ]));

  const rows = await trustedRows(["a", "b"], [ME]);

  expect(rows).toEqual([{ account: ME, labels: ["a", "b"], description: JSON.stringify(corpus), modified: "2026-10-12T09:00:00+00:00" }]);
  const query = new URL(`http://x${sent[0]!.query}`).searchParams;
  expect(query.getAll("filter").map((each) => JSON.parse(each) as unknown)).toEqual([
    { field: "labels", op: "is", value: "a" },
    { field: "labels", op: "is", value: "b" },
  ]);
});

test("filed rows keep every account's, and drop a row that names none", async () => {
  stubFetch(() => Response.json([
    { ...row(corpus, ["a"], "mallory@example.com"), modified: "2026-10-12T09:00:00+00:00" },
    { ...row(corpus, ["a"]), account: null },
  ]));

  expect((await filedRows(["a"])).map(({ account }) => account)).toEqual(["mallory@example.com"]);
});
