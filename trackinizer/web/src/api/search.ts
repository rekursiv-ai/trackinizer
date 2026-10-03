import { type CallOptions, client, send, TIMEOUT_MS } from "./client";
import type { DetailRow } from "./detail";
import { inquiryKinds } from "./inquiries";

/** One search, of one kind or of every kind. */
export type SearchRequest<Field extends string = string> = {
  /** The search box's text, sent as typed. */
  readonly q: string;
  /** PascalCase, as the server's kind list spells it: `Issue`; unset, every kind. */
  readonly kind?: string;
  readonly limit: number;
  /** The only keys each hit carries; unset, every key. A key another kind owns is absent. */
  readonly fields?: readonly Field[];
};

/** A hit with the keys `Field` names, or a whole row when the search named none. */
export type SearchHit<Field extends string = string> = string extends Field ? DetailRow : Pick<DetailRow, Field>;

/**
 * Search `kind`, or every kind, with `GET /api/web/search`: rows whose title or
 * description match `q`, newest created first, each shaped as `/api/web/get`'s
 * `self`.
 *
 * The grammar is the server's (`_parse_query` in `server/web.py`): terms split on
 * space, tab, CR and LF and must all match; a bare term is a case-insensitive
 * substring of the title or the description; `title:RE` and `description:RE` are
 * case-insensitive regexes on one field, which the server checks with Python's
 * `re` before PostgreSQL runs them; only `"` groups a phrase, adding nothing
 * itself, and `'` and `\` are ordinary characters. The palette asks one kind per
 * request; the search page asks every kind at once (0.21 s median on production,
 * 2026-10-01), where in 2026-09 one search across kinds took 2.3 s to the
 * server's 5 s budget. A search over the budget, a bad regex or an unclosed
 * quote is an `ApiError` with status 400 and the server's message. A kind this
 * build does not know throws before asking (`inquiryKinds`).
 */
export async function searchInquiries<Field extends string = string>(
  { q, kind, limit, fields }: SearchRequest<Field>,
  { signal }: CallOptions = {},
): Promise<SearchHit<Field>[]> {
  const [one] = kind === undefined ? [] : inquiryKinds([kind]);
  const rows = await send(TIMEOUT_MS.search, signal, (signal) =>
    client.GET("/api/web/search", {
      params: { query: { q, ...(one && { kind: one }), limit, ...(fields && { fields: [...fields] }) } },
      signal,
    }),
  );
  // Typed as free JSON by the schema; `_row_to_dict` in `server/web.py` builds each.
  return rows as SearchHit<Field>[];
}
