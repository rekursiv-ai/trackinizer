import { execFile, execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";
import { expect, test } from "vitest";

const BASE = "/app/";

/** One chunk as Vite's manifest describes it; imports are other manifest keys. */
type ManifestChunk = { file: string; css?: string[]; imports?: string[]; dynamicImports?: string[] };

/** The app built in memory, once for the file: each output file's text by name. */
function built(): Promise<Map<string, string>> {
  return (building ??= build({ root: fileURLToPath(new URL("..", import.meta.url)), logLevel: "silent", build: { write: false } }).then(
    (result) => {
      if (Array.isArray(result) || !("output" in result)) {
        throw new Error("Expected a single application bundle");
      }
      return new Map(result.output.map((file) => [file.fileName, file.type === "chunk" ? file.code : file.source.toString()]));
    },
  ));
}
let building: Promise<Map<string, string>> | undefined;

test("the build serves from /app/ and records its commit", { tags: ["manual"] }, async () => {
  const root = fileURLToPath(new URL("..", import.meta.url));
  const files = await built();

  const references = [
    ...files.get("index.html")!.matchAll(/(?:src|href)="([^"]+)"/g),
  ].map(([, reference]) => reference!);
  for (const extension of [".js", ".svg"]) {
    expect(references.some((r) => r.endsWith(extension)), extension).toBe(true);
  }
  for (const reference of references) {
    expect(reference.startsWith(BASE), reference).toBe(true);
    expect(files.has(reference.slice(BASE.length)), reference).toBe(true);
  }
  // The entry loads the app dynamically (src/main.tsx), so index.html names no
  // stylesheet: the entry preloads the app's, and the app renders once they load.
  const manifest = JSON.parse(files.get(".vite/manifest.json")!) as { [key: string]: ManifestChunk };
  const sheets = manifest["src/start.tsx"]?.css ?? [];
  expect(sheets).not.toEqual([]);
  for (const sheet of sheets) {
    expect(files.has(sheet), sheet).toBe(true);
    expect(files.get(manifest["index.html"]!.file), sheet).toContain(`"${sheet}"`);
  }

  expect(JSON.parse(files.get("version.json")!)).toEqual({
    commit: execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: root,
      encoding: "utf8",
    }).trim(),
    openapi_sha256: createHash("sha256")
      .update(readFileSync(join(root, "src/api/openapi.json")))
      .digest("hex"),
  });
});

test("index.html preloads every chunk a first visit loads, so none waits for the entry to run", { tags: ["manual"] }, async () => {
  const files = await built();
  const manifest = JSON.parse(files.get(".vite/manifest.json")!) as { [key: string]: ManifestChunk };
  // What a first visit loads, as scripts/bundle-size.ts counts it: the entry and
  // its imports, then the chunks it imports dynamically and theirs.
  const firstLoad = new Set<string>();
  const load = (key: string) => {
    const chunk = manifest[key]!;
    if (firstLoad.has(chunk.file)) return;
    firstLoad.add(chunk.file);
    chunk.imports?.forEach(load);
  };
  load("index.html");
  manifest["index.html"]!.dynamicImports?.forEach(load);
  const html = files.get("index.html")!;
  const entry = [...html.matchAll(/<script type="module" crossorigin src="([^"]+)">/g)].map(([, src]) => src!);
  const preloaded = [...html.matchAll(/<link rel="modulepreload" crossorigin href="([^"]+)">/g)].map(([, href]) => href!);
  expect([...entry, ...preloaded].toSorted()).toEqual([...firstLoad].map((file) => `${BASE}${file}`).toSorted());
});

/**
 * Run a copy of the `npm` wrapper in `web` with a fake `uv` that names
 * `nodeBin` as the wheel's Node, whose `node` prints the shim it was reached
 * through (the first entry on its PATH). Returns that shim.
 */
function runWrapper(web: string, nodeBin: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      join(web, "npm"),
      ["--version"],
      { env: { ...process.env, PATH: `${join(web, "..", "fake-bin")}:${process.env.PATH}`, FAKE_NODE_BIN: nodeBin } },
      (error, stdout) => (error ? reject(error) : resolve(stdout.trim())),
    );
  });
}

/** A fake wheel: `bin/node`, which prints the first PATH entry, and npm's CLI beside it. */
function fakeWheel(root: string, name: string): string {
  const bin = join(root, name, "bin");
  mkdirSync(join(root, name, "lib/node_modules/npm/bin"), { recursive: true });
  mkdirSync(bin, { recursive: true });
  writeFileSync(join(bin, "node"), '#!/bin/sh\necho "${PATH%%:*}"\n', { mode: 0o755 });
  return bin;
}

test("the npm wrapper never removes a shim another run may have on its PATH (WEB-04)", { tags: ["manual"] }, async () => {
  const root = mkdtempSync(join(tmpdir(), "npm-wrapper-"));
  const web = join(root, "web");
  mkdirSync(web);
  copyFileSync(fileURLToPath(new URL("../npm", import.meta.url)), join(web, "npm"));
  chmodSync(join(web, "npm"), 0o755);
  mkdirSync(join(root, "fake-bin"));
  writeFileSync(join(root, "fake-bin", "uv"), '#!/bin/sh\necho "$FAKE_NODE_BIN"\n', { mode: 0o755 });
  const [first, second] = [fakeWheel(root, "wheel-a"), fakeWheel(root, "wheel-b")];

  // Starts at once for one wheel share one shim, whole.
  const shims = await Promise.all(Array.from({ length: 6 }, () => runWrapper(web, first)));
  expect(new Set(shims).size).toBe(1);
  const inUse = shims[0]!;
  expect(realpathSync(join(inUse, "node"))).toBe(realpathSync(join(first, "node")));

  // A start for another wheel, as after `uv sync` while an e2e run is under way,
  // leaves the running one's shim as it was.
  const other = await runWrapper(web, second);
  expect(other).not.toBe(inUse);
  expect(realpathSync(join(inUse, "node"))).toBe(realpathSync(join(first, "node")));
  expect(realpathSync(join(other, "node"))).toBe(realpathSync(join(second, "node")));
});
