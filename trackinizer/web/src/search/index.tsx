import { useQuery } from "@tanstack/react-query";
import { searchInquiries } from "../api/search";
import { useRouter } from "../router/router";
import { ReadFailure } from "../ui/failure";
import { capitalize, RefChip, StateGlyphs } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { EmptyState, ViewHeader } from "../ui/view";
// The ref chip's style (`.ref`) is the Markdown's, which this page does not render.
import "../markdown/markdown.css";
import "./search.css";

/**
 * The search results page, `#/search/<q>`, as the old UI's was: one search
 * across every kind, the newest 50 matches, in a table of ref, status and
 * title, with their count. The server's message shows in place of the table,
 * a search over its time budget included. Results are a snapshot of when the
 * search ran, as the palette's are.
 */
export function SearchView({ q }: { q: string }) {
  const { navigate } = useRouter();
  const search = useQuery({
    queryKey: ["search", q, null],
    queryFn: ({ signal }) => searchInquiries({ q, limit: LIMIT, fields: FIELDS }, { signal }),
    enabled: q !== "",
    staleTime: Infinity,
  });
  const hits = search.data;
  return (
    <div className="view search">
      <ViewHeader icon={<Icon name="search" />} title={q ? `Search: ${q}${hits ? ` (${hits.length})` : ""}` : "Search"}>
        <form
          role="search"
          onSubmit={(event) => {
            event.preventDefault();
            navigate({ name: "search", q: String(new FormData(event.currentTarget).get("q")).trim() });
          }}
        >
          <input
            key={q}
            className="field"
            type="search"
            name="q"
            aria-label="Search query"
            placeholder='Terms, "a phrase", title:re or description:re'
            defaultValue={q}
            autoComplete="off"
            autoFocus={!q}
          />
        </form>
      </ViewHeader>
      <div className="scroll">
        {!q ? (
          <EmptyState icon={<Icon name="search" size={24} />} title="Enter a query above" />
        ) : hits ? (
          hits.length ? (
            <table className="search-t">
              <thead>
                <tr>
                  <th>Ref</th>
                  <th>Status</th>
                  <th>Title</th>
                </tr>
              </thead>
              <tbody>
                {hits.map((hit) => (
                  <tr key={hit.id}>
                    <td>
                      <RefChip kind={hit.kind} seq={hit.seq} title={hit.title} />
                    </td>
                    <td>
                      <span className="search-state">
                        <StateGlyphs status={hit.status} judgement={typeof hit.judgement === "string" ? hit.judgement : null} />
                        {capitalize(hit.status)}
                      </span>
                    </td>
                    <td>{hit.title}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState icon={<Icon name="search" size={24} />} title="No matches" />
          )
        ) : search.isError ? (
          <ReadFailure error={search.error} retry={() => void search.refetch()} />
        ) : (
          <p className="search-note" aria-busy="true">
            Searching…
          </p>
        )}
      </div>
    </div>
  );
}

/** The newest matches the page shows, as the old UI's search did. */
const LIMIT = 50;
/** The keys the table reads. */
const FIELDS = ["id", "kind", "seq", "title", "status", "judgement"] as const;
