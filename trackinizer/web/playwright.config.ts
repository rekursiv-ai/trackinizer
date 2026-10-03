import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

// The e2e script's default port is also 8797.
const port = Number(process.env.TRACKINIZER_E2E_PORT ?? 8797);
// End-to-end runs serve their own build, which the e2e script writes to
// dist-e2e/<port>/: dist/ is rewritten under them by the local preview's
// `vite build --watch`, and a run on another port empties its own directory,
// not this one. A missing build fails here rather than as a server that answers
// 404 until the start timeout.
const dist = fileURLToPath(new URL(`dist-e2e/${port}/`, import.meta.url));
if (!existsSync(join(dist, "index.html"))) {
  throw new Error(`No build in ${dist}; run the e2e script, which builds it.`);
}

export default defineConfig({
  testDir: "./e2e",
  // One directory per port, so runs on different ports never share results.
  outputDir: join(tmpdir(), `trackinizer-web-e2e-${port}`),
  timeout: 30_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
  },
  webServer: {
    command: [
      "uv --quiet run --frozen python -m trackinizer.server",
      `--ephemeral --no-auth --app-dir '${dist}' --port ${port}`,
    ].join(" "),
    cwd: fileURLToPath(new URL("../../..", import.meta.url)),
    env: {
      // A second user, an admin, so the admin spec has someone other than
      // itself to change: the server refuses an admin's own demotion, and only
      // sign-in and this bootstrap create users.
      TRACKINIZER_BOOTSTRAP_ADMIN: "e2e-admin@example.com",
      // The bootstrap writes that user's token here, not into the real
      // per-user data directory.
      TRACKINIZER_BOOTSTRAP_TOKEN_FILE: join(tmpdir(), `trackinizer-web-e2e-${port}-bootstrap-token`),
    },
    url: `http://127.0.0.1:${port}/app/`,
    // Another checkout's server on this port would serve a different build.
    reuseExistingServer: false,
    timeout: 120_000,
  },
  // The hubs project makes the big hubs before any spec file starts (see
  // e2e/hubs.setup.ts); a run of one spec file still runs it first.
  projects: [
    { name: "hubs", testMatch: /hubs\.setup\.ts/ },
    { name: "chromium", use: { ...devices["Desktop Chrome"] }, dependencies: ["hubs"] },
  ],
});
