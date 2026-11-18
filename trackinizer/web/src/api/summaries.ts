import { parseCorpusDoc, parseSessionDoc, type CorpusDoc, type SessionDoc } from "../visuals/summaryDoc";
import type { CallOptions } from "./client";
import { listInquiries } from "./inquiries";
import { getProfile } from "./me";

/** A campaign's summary: its overview across sessions, and each session's. */
export type CampaignSummary = {
  readonly corpus: CorpusDoc | null;
  readonly sessions: ReadonlyMap<string, SessionDoc>;
};

/** The most summary rows one campaign files: a session and the campaign each; a campaign has far fewer sessions. */
const ROWS = 400;

/**
 * Read a campaign's summary from the record a canvas shows: the brief Issue, or any session of it.
 *
 * The summary service files each session's and the campaign's summary as an Artifact, labelled
 * `summary:session:<id>`, `summary:issue:<id>` and `summary:lead:<id>`; these are the same rows every
 * signed-in user reads, kept only when filed by one of `accounts`. A record with no summary yet answers `null`.
 */
export async function loadCampaignSummary(
  recordId: string,
  accounts: readonly string[],
  { signal }: CallOptions = {},
): Promise<CampaignSummary | null> {
  const byIssue = await trustedRows([`summary:issue:${recordId}`], accounts, { signal });
  if (byIssue.length > 0) return assemble(byIssue);
  const own = await trustedRows([`summary:session:${recordId}`], accounts, { signal });
  const lead = own.flatMap((row) => row.labels).find((label) => label.startsWith("summary:lead:"));
  return lead ? assemble(await trustedRows([lead], accounts, { signal })) : null;
}

/**
 * What the page may rely on: the audiences the caller reads (`staff` is the transcript's), and the
 * accounts whose summary rows it trusts. Any writer can label a row of their own `summary:...`, so
 * a row counts only if the server attests it was filed by one of the staff setting's service accounts.
 */
export type Trust = { readonly audiences: readonly string[]; readonly accounts: readonly string[] };

export async function readTrust({ signal }: CallOptions = {}): Promise<Trust> {
  const profile: Readonly<Record<string, unknown>> = await getProfile({ signal });
  const strings = (value: unknown) => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);
  return { audiences: strings(profile.read_audiences), accounts: strings(profile.summary_accounts) };
}

/** An Artifact found by its labels: the account that filed it, its labels, the document its description holds, and when it last changed. */
export type FiledRow = {
  readonly account: string;
  readonly labels: readonly string[];
  readonly description: string | null;
  readonly modified: string;
};

/** The Artifacts that carry every one of `labels`, whoever filed them; the caller says whose it trusts. */
export async function filedRows(labels: readonly string[], { signal }: CallOptions = {}): Promise<FiledRow[]> {
  const rows = await listInquiries({
    kinds: ["Artifact"],
    filters: labels.map((label) => ({ field: "labels", op: "is", value: label })),
    limit: ROWS,
    offset: 0,
  }, { signal });
  return rows.flatMap((row) => {
    const { description, account } = row as unknown as { readonly description?: unknown; readonly account?: unknown };
    if (typeof account !== "string") return [];
    return [{ account, labels: row.labels ?? [], description: typeof description === "string" ? description : null, modified: row.modified }];
  });
}

/** The Artifacts that carry every one of `labels`, kept only when filed by one of `accounts`. */
export async function trustedRows(
  labels: readonly string[],
  accounts: readonly string[],
  { signal }: CallOptions = {},
): Promise<FiledRow[]> {
  return (await filedRows(labels, { signal })).filter(({ account }) => accounts.includes(account));
}

function assemble(rows: readonly FiledRow[]): CampaignSummary | null {
  let corpus: CorpusDoc | null = null;
  const sessions = new Map<string, SessionDoc>();
  for (const row of rows) {
    const doc = parseSessionDoc(row.description);
    if (doc) {
      sessions.set(doc.session, doc);
      continue;
    }
    corpus ??= parseCorpusDoc(row.description);
  }
  return corpus || sessions.size > 0 ? { corpus, sessions } : null;
}
