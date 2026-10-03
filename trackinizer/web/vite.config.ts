import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vitest/config";

// The dev server stands in for the trackinizer server: it proxies the API and the
// login pages to TRACKINIZER_WEB_API_URL, and signs each request with
// TRACKINIZER_WEB_API_TOKEN when set. A bearer token takes precedence over the
// session cookie. These are not the trax CLI's TRACKINIZER_URL and
// TRACKINIZER_TOKEN on purpose: a shell set up for trax would otherwise point a
// dev build at production with a full-write token. Against production, use a
// token capped at the viewer role.
const token = process.env.TRACKINIZER_WEB_API_TOKEN;
const proxyRoute = {
  target: process.env.TRACKINIZER_WEB_API_URL ?? "http://127.0.0.1:8765",
  changeOrigin: true,
  headers: token ? { Authorization: `Bearer ${token}` } : {},
};

// The commit this app is built from. The bundle carries it for Copy details
// (src/debug/details.ts) and dist/version.json for the deploy: read at run time,
// version.json could already name a newer build than the one the tab loaded.
const commit = execFileSync("git", ["rev-parse", "HEAD"], {
  cwd: fileURLToPath(new URL(".", import.meta.url)),
  encoding: "utf8",
}).trim();

export default defineConfig({
  base: "/app/",
  plugins: [react(), versionJson(), preloadFirstLoad()],
  define: { __COMMIT__: JSON.stringify(commit) },
  build: {
    // dist/.vite/manifest.json, from which scripts/bundle-size.ts reads what a
    // first visit loads: the entry imports React DOM and the app dynamically
    // (src/main.tsx), so index.html alone no longer names it.
    manifest: true,
  },
  server: {
    proxy: { "/api": proxyRoute, "/auth": proxyRoute },
  },
  test: {
    // The app's logger (src/debug/log.ts) prints every failed request, and the
    // screen tests fail hundreds on purpose; its own tests read the console.
    onConsoleLog: (line) => !line.startsWith("trackinizer "),
    // A worker runs many test files; src/testSetup.ts gives each what a fresh
    // worker would.
    isolate: false,
    // Every test that runs by itself takes under 100 ms warm on x86. One that
    // cannot is tagged `manual`, which skips it unless `npm run test:manual` sets
    // TRACKINIZER_WEB_MANUAL_TESTS and picks the manual tests out; only that run
    // has the build tier, which runs Vite itself.
    tags: [
      {
        name: "manual",
        description: "Over 100 ms: runs only through npm run test:manual.",
        skip: process.env.TRACKINIZER_WEB_MANUAL_TESTS !== "1",
      },
    ],
    projects: [
      {
        extends: true,
        test: {
          name: "unit",
          environment: "jsdom",
          include: ["src/**/*.test.{ts,tsx}"],
          exclude: ["src/build.test.ts"],
          setupFiles: ["src/testSetup.ts"],
        },
      },
      ...(process.env.TRACKINIZER_WEB_MANUAL_TESTS === "1"
        ? [{ extends: true, test: { name: "build", environment: "node", include: ["src/build.test.ts"] } }]
        : []),
    ],
  },
});

function versionJson(): Plugin {
  return {
    name: "trackinizer-version-json",
    apply: "build",
    generateBundle() {
      // The schema this build is typed against. The deploy compares it with the
      // live /openapi.json, normalised by scripts/openapi_dump.py, which prints
      // the same hash; a mismatch means the server and the app would disagree.
      const openapi_sha256 = createHash("sha256")
        .update(readFileSync(new URL("src/api/openapi.json", import.meta.url)))
        .digest("hex");
      this.emitFile({
        type: "asset",
        fileName: "version.json",
        source: `${JSON.stringify({ commit, openapi_sha256 })}\n`,
      });
    },
  };
}

/**
 * A `<link rel="modulepreload">` in index.html for every chunk the entry imports
 * dynamically, and every chunk those import: React DOM and the app (src/main.tsx).
 *
 * Vite preloads the entry's static imports, but a dynamic import's chunks only
 * once the entry has run and asks for them, a round trip after the entry
 * arrives. Named here, they download beside it.
 */
function preloadFirstLoad(): Plugin {
  let base = "/";
  return {
    name: "trackinizer-preload-first-load",
    apply: "build",
    configResolved(config) {
      base = config.base;
    },
    transformIndexHtml: {
      order: "post",
      handler(_html, { bundle, chunk: entry }) {
        if (!bundle || !entry) return;
        const chunks = new Map(Object.values(bundle).flatMap((file) => (file.type === "chunk" ? [[file.fileName, file]] : [])));
        const reached = (roots: readonly string[], into = new Set<string>()): Set<string> => {
          for (const file of roots) {
            if (into.has(file)) continue;
            into.add(file);
            reached(chunks.get(file)?.imports ?? [], into);
          }
          return into;
        };
        // The entry and its imports, which index.html already loads.
        const preloaded = reached([entry.fileName]);
        return [...reached(entry.dynamicImports)]
          .filter((file) => !preloaded.has(file))
          .map((file) => ({ tag: "link", attrs: { rel: "modulepreload", crossorigin: true, href: `${base}${file}` }, injectTo: "head" }));
      },
    },
  };
}
