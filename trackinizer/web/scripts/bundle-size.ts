// Fail when the JavaScript a first visit loads exceeds the budget, gzip-compressed.
//
// First-load JavaScript is the entry, the chunks it imports dynamically, and every
// chunk those import. The entry (src/main.tsx) imports React DOM and the app
// dynamically at once, so that each is evaluated in a task of its own; the
// views the app imports dynamically load when first shown and do not count.
// Vite's manifest (build.manifest) names each chunk's imports. Gzip level 9
// matches the 2026-09-26 library survey the budget was set from.
//
// Usage: node scripts/bundle-size.ts [distDir], distDir defaulting to dist/.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";

/** One chunk as Vite's manifest describes it; imports are other manifest keys. */
type Chunk = { readonly file: string; readonly imports?: readonly string[]; readonly dynamicImports?: readonly string[] };

const BUDGET_KB = 250;
const dist = process.argv[2] ? pathToFileURL(`${resolve(process.argv[2])}/`) : new URL("../dist/", import.meta.url);
const manifest = JSON.parse(readFileSync(new URL(".vite/manifest.json", dist), "utf8")) as { [key: string]: Chunk };
const entry = manifest["index.html"];
if (!entry) throw new Error("The build's manifest has no index.html entry.");

const files = new Set<string>();
const load = (key: string) => {
  const chunk = manifest[key]!;
  if (files.has(chunk.file)) return;
  files.add(chunk.file);
  chunk.imports?.forEach(load);
};
load("index.html");
entry.dynamicImports?.forEach(load);

let totalKb = 0;
for (const path of files) {
  const kb = gzipSync(readFileSync(new URL(path, dist)), { level: 9 }).length / 1024;
  totalKb += kb;
  console.log(`${kb.toFixed(1).padStart(7)} KB  ${path}`);
}
console.log(`${totalKb.toFixed(1).padStart(7)} KB  first-load JS, gzip (budget ${BUDGET_KB} KB)`);
if (totalKb > BUDGET_KB) {
  console.error(`First-load JavaScript exceeds the ${BUDGET_KB} KB budget.`);
  process.exitCode = 1;
}
