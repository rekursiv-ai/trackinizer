// The live suite's harness on its own: its server and writer processes end, and
// fail, within bounds, so a broken run fails instead of hanging.
import { existsSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, test } from "@playwright/test";
import { answers, LiveServer, sleep, Writer } from "./harness";

test.describe.configure({ timeout: 15_000 });

test("a server that fails to start leaves no data behind (CR-LIVE-R4-D1)", async () => {
  let data = "";
  const exits = (_: number, dir: string) => {
    data = dir;
    return ["node", ["-e", "process.exit(3)"]] as const;
  };
  await expect(LiveServer.start(exits)).rejects.toThrow("did not start");
  expect(data).not.toBe("");
  expect(existsSync(data)).toBe(false);
});

test("stopping a server a signal has already ended returns (CR-LIVE-R3-03)", async () => {
  // Serves /app/, then ends itself with SIGKILL, which leaves no exit code.
  const killed = (port: number) =>
    [
      "node",
      [
        "-e",
        `require("node:http").createServer((_, r) => r.end("ok")).listen(${port}, "127.0.0.1");` +
          `setTimeout(() => process.kill(process.pid, "SIGKILL"), 300);`,
      ],
    ] as const;
  const server = await LiveServer.start(killed);
  await sleep(1_000);
  await server.close();
});

test("a readiness probe of a server that takes the request and never answers gives up (CR-LIVE-R7-B1)", async () => {
  const silent = createServer(() => {});
  await new Promise<void>((listening) => silent.listen(0, "127.0.0.1", listening));
  const from = Date.now();
  expect(await answers(`http://127.0.0.1:${(silent.address() as AddressInfo).port}/app/`)).toBe(false);
  expect(Date.now() - from).toBeLessThan(3_000);
  silent.closeAllConnections();
  silent.close();
});

test("a writer that exits fails the command waiting on it, and every later one (CR-LIVE-03)", async () => {
  const writer = new Writer("unused", "false");
  await expect(writer.send({ op: "edit" })).rejects.toThrow("The writer exited");
  await expect(writer.send({ op: "edit" })).rejects.toThrow("The writer exited");
});
