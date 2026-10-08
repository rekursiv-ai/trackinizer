import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SettingsView } from ".";
import { FAST, profile, renderScreen, serveAccount, stubClipboard } from "./testing";

const ADA = profile("writer");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function section() {
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const region = within(screen.getByRole("region", { name: "Chat partner" }));
  await region.findByText("scout");
  return region;
}

test("the shared assistant is chosen by default and named; the local commands stay hidden", async () => {
  serveAccount(ADA);
  const region = await section();
  expect(region.getByRole("radio", { name: /Shared assistant/ })).toHaveProperty("checked", true);
  expect(region.getByRole("radio", { name: /My local helper/ })).toHaveProperty("checked", false);
  expect(region.queryByText("trax helper claude")).toBeNull();
});

test("an assistant that is not set up is said to be missing", async () => {
  serveAccount(ADA, { workspace: { assistant: null } });
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const region = within(screen.getByRole("region", { name: "Chat partner" }));
  await region.findByText("not set up on this server");
});

test("choosing the local helper writes a partner operation at the canvas's revision and shows the commands", async () => {
  const server = serveAccount(ADA);
  const copied = stubClipboard();
  const region = await section();
  fireEvent.click(region.getByRole("radio", { name: /My local helper/ }));
  await waitFor(() => expect(server.writes()).toContainEqual({
    call: "POST /api/workspaces/w-ada/operations", body: { revision: 4, operation: { kind: "partner", choice: "local" } },
  }), FAST);
  await waitFor(() => expect(region.getByRole("radio", { name: /My local helper/ })).toHaveProperty("checked", true), FAST);
  const setup = `uv tool install trackinizer && trax profile url to ${location.origin} && trax profile token to <TOKEN>`;
  expect(region.getByText(setup)).toBeTruthy();
  expect(region.getByText("trax helper claude")).toBeTruthy();
  fireEvent.click(region.getAllByRole("button", { name: "Copy" })[0]!);
  await waitFor(() => expect(copied).toEqual([setup]), FAST);
});

test("choosing the shared assistant again writes the shared choice", async () => {
  const server = serveAccount(ADA, { workspace: { partner_choice: "local" } });
  const region = await section();
  fireEvent.click(region.getByRole("radio", { name: /Shared assistant/ }));
  await waitFor(() => expect(server.writes()).toContainEqual({
    call: "POST /api/workspaces/w-ada/operations", body: { revision: 4, operation: { kind: "partner", choice: "shared" } },
  }), FAST);
  await waitFor(() => expect(region.queryByText("trax helper claude")).toBeNull(), FAST);
});

test("a refused switch says so and leaves the choice where it was", async () => {
  const server = serveAccount(ADA);
  server.answers.push(() => Response.json({ detail: "revision conflict" }, { status: 409 }));
  const region = await section();
  fireEvent.click(region.getByRole("radio", { name: /My local helper/ }));
  await region.findByText("Could not change the Chat partner.");
  expect(region.getByRole("radio", { name: /Shared assistant/ })).toHaveProperty("checked", true);
});
