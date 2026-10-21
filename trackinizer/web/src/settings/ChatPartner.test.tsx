import { cleanup, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { SettingsView } from ".";
import { profile, renderScreen, serveAccount } from "./testing";

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

test("the shared assistant is the partner, and it is named; no local helper is offered", async () => {
  serveAccount(ADA);
  const region = await section();
  expect(region.getByText("Shared assistant")).toBeTruthy();
  expect(region.queryByRole("radio")).toBeNull();
  expect(region.queryByText(/local helper/)).toBeNull();
  expect(region.queryByText(/helper|uv tool install/)).toBeNull();
});

test("an assistant that is not set up is said to be missing", async () => {
  serveAccount(ADA, { workspace: { assistant: null } });
  renderScreen(<SettingsView assign={vi.fn()} />, ADA);
  const region = within(screen.getByRole("region", { name: "Chat partner" }));
  await region.findByText("not set up on this server");
});
