import { FILES_LABEL, FILES_OF, type FilesDoc, parseFilesDoc } from "../visuals/filesDoc";
import { ApiError, type CallOptions } from "./client";
import { getInquiry } from "./inquiries";
import { filedRows } from "./summaries";

/** A document filed for a record, and how many readable ones are filed for it. */
export type Found<Doc> = { readonly doc: Doc; readonly count: number };

/** A record's files document, and how many readable ones are filed for it. */
export type FilesFound = Found<FilesDoc>;

/**
 * Read the files document filed for `record`: the Artifacts labelled `files` and
 * `files:of:<record>`, and each of `labels` too, by `loadFiled`'s rule.
 */
export async function loadFiles(
  record: string,
  accounts: readonly string[],
  { labels = [], signal }: CallOptions & { readonly labels?: readonly string[] } = {},
): Promise<FilesFound | null> {
  return loadFiled(record, [FILES_LABEL, `${FILES_OF}${record}`, ...labels], accounts, parseFilesDoc, { signal });
}

/**
 * Read the document filed for `record` under every one of `labels`, kept only
 * when filed by one of `accounts` (the operator's service accounts) or by the
 * account `record` itself is filed under, since any writer can put these labels
 * on a row; of those that `parse` reads, the newest changed. Null when none is
 * filed.
 *
 * So a person's own source (a work tree they file on their own session or Issue)
 * shows with no change here, and no one else's can be planted on their record.
 * The record is read only when some row is filed by another account than
 * `accounts`. The list route reads only what the reader may, so a document shows
 * only to one who may read its Artifact.
 */
export async function loadFiled<Doc>(
  record: string,
  labels: readonly string[],
  accounts: readonly string[],
  parse: (text: string | null) => Doc | null,
  { signal }: CallOptions = {},
): Promise<Found<Doc> | null> {
  const rows = await filedRows(labels, { signal });
  const owner = rows.some(({ account }) => !accounts.includes(account)) ? await accountOf(record, { signal }) : null;
  const docs = rows.flatMap(({ account, description, modified }) => {
    const doc = accounts.includes(account) || account === owner ? parse(description) : null;
    return doc ? [{ doc, at: Date.parse(modified) }] : [];
  });
  const newest = docs.reduce<(typeof docs)[number] | null>((best, each) => (best === null || each.at > best.at ? each : best), null);
  return newest ? { doc: newest.doc, count: docs.length } : null;
}

/** The account `record` is filed under, or null when the reader may not read it. */
async function accountOf(record: string, { signal }: CallOptions): Promise<string | null> {
  try {
    const { account } = await getInquiry(record, { signal });
    return typeof account === "string" ? account : null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}
