import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { TestInfo } from "@playwright/test";

// hubs.setup.ts makes these before any spec file starts; see why there.
export const DETAIL_CHILDREN = 61;
export const PERF_CHILDREN = 60;

/** Where the setup project leaves the hubs' ids: the run's own output directory. */
export const hubsFile = (info: TestInfo) => join(info.project.outputDir, "hubs.json");

/** The detail hub's batch ids, in item order, and the performance hub's id. */
export function readHubs(info: TestInfo): { detail: string[]; perf: string } {
  return JSON.parse(readFileSync(hubsFile(info), "utf8"));
}
