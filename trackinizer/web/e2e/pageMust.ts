// `must` for code that `page.evaluate` runs in the page, which cannot import it:
// the page gets the same function, as `pageMust` (declared in `page.d.ts`).
import { must } from "../src/must";

/** The init script that defines `pageMust` in every page of a context it is added to. */
export const PAGE_MUST = { content: `globalThis.pageMust = ${must.toString()};` };
