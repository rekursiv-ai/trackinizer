import type { Command } from "../commands/registry";
import { formatRoute } from "../router/route";
import { type InquiryRoute, jumpsFor, parseSearch } from "./grammar";
import type { Found, KindSearch } from "./sources";

/** One row of the palette's listbox, under its section's heading. */
export type Option = { readonly key: string; readonly section: string } & (
  /** An inquiry to open; `found` is `null` for a jump to one the palette has not loaded. */
  | { readonly type: "inquiry"; readonly route: InquiryRoute; readonly found: Found | null }
  | { readonly type: "command"; readonly command: Command }
  /** Search `kinds` again, after each failed with `message`. */
  | { readonly type: "retry"; readonly message: string; readonly kinds: readonly string[]; readonly retry: () => void }
);

/** The heading of the server's results. */
export const SERVER_SECTION = "From the server";

/** What the palette can list, from what the app holds and what the server answered. */
export type Sources = {
  /** The search box, as typed. */
  readonly text: string;
  /** Every inquiry kind, in the server's order. */
  readonly kinds: readonly string[];
  /** Every inquiry the cache holds, most recently modified first. */
  readonly cached: readonly Found[];
  /** Inquiries opened lately, newest first. */
  readonly recent: readonly Found[];
  /** The mounted commands; those with a section are listed. */
  readonly commands: readonly Command[];
  /** The server's answers so far, one per kind, in the order they were asked. */
  readonly searches: readonly KindSearch[];
};

/**
 * The palette's rows for `sources`, as the mock's ⌘K palette lists them.
 *
 * With no text: recently opened inquiries, then every command. With text: jumps
 * to a named `Kind#seq` or UUID, loaded inquiries that match, matching commands,
 * then the server's results, kind by kind in the order asked, and one Retry row
 * per distinct failure. Server results come last, so their late arrival never
 * moves a row above them. An inquiry is listed once, at its first place.
 */
export function paletteOptions({ text, kinds, cached, recent, commands, searches }: Sources): Option[] {
  const q = text.trim();
  const options: Option[] = [];
  const listed = new Set<string>();
  const addInquiry = (section: string, found: Found) => {
    listed.add(found.id);
    options.push({ key: `inquiry:${found.id}`, section, type: "inquiry", route: refOf(found), found });
  };
  // Four recent and eight loaded rows, as the mock lists.
  if (!q) {
    recent.slice(0, 4).forEach((found) => addInquiry("Recently opened", found));
  } else {
    for (const route of jumpsFor(q, kinds)) {
      const found = cached.find((row) => named(route, row)) ?? null;
      if (found) addInquiry("Jump to", found);
      else options.push({ key: `jump:${formatRoute(route)}`, section: "Jump to", type: "inquiry", route, found });
    }
    const matches = parseSearch(q);
    cached
      .filter((found) => !listed.has(found.id) && matches(found))
      .slice(0, 8)
      .forEach((found) => addInquiry("Inquiries", found));
  }
  options.push(...commandOptions(commands, q.toLowerCase()));
  for (const search of searches) {
    search.hits?.filter((found) => !listed.has(found.id)).forEach((found) => addInquiry(SERVER_SECTION, found));
  }
  const failures = new Map<string, KindSearch[]>();
  for (const search of searches) {
    if (search.error) failures.set(search.error.message, [...(failures.get(search.error.message) ?? []), search]);
  }
  for (const [message, failed] of failures) {
    options.push({
      key: `retry:${message}`,
      section: SERVER_SECTION,
      type: "retry",
      message,
      kinds: failed.map((search) => search.kind),
      retry: () => failed.forEach((search) => search.retry()),
    });
  }
  return options;
}

/** Commands with a section whose title has `needle`, grouped by section in first-mounted order. */
function commandOptions(commands: readonly Command[], needle: string): Option[] {
  const matching = commands.filter((command) => command.section && command.title.toLowerCase().includes(needle));
  const sections = [...new Set(matching.map((command) => command.section!))];
  return sections.flatMap((section) =>
    matching
      .filter((command) => command.section === section)
      .map((command) => ({ key: `command:${command.id}`, section, type: "command", command }) as const),
  );
}

function refOf(found: Found): InquiryRoute {
  return { name: "ref", kind: found.kind, seq: found.seq };
}

function named(route: InquiryRoute, found: Found): boolean {
  return route.name === "lookup" ? found.id === route.id : found.kind === route.kind && found.seq === route.seq;
}
