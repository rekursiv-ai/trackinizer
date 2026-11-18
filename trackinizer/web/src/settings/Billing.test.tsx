import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { BalanceBody } from "../api/billing";
import { stubFetch } from "../api/testing";
import { BillingSection } from "./Billing";
import { profile, renderScreen } from "./testing";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const BALANCE: BalanceBody = { usd: 498.2, granted_usd: 500, purchased_usd: 25, spent_usd: 26.8, topup_usd: [10, 25], enforced: true, grace_until: null, own_login: false,
  can_run: true, reason: "You have credit left." };

function serve(balance: BalanceBody | null, topup: () => Response = () => Response.json({ url: "https://checkout.stripe.test/pay/cs_1" })) {
  return stubFetch((request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/addons/billing/balance") return balance ? Response.json(balance) : Response.json({ detail: "Not Found" }, { status: 404 });
    if (path === "/api/addons/billing/topup") return topup();
    return Response.json({}, { status: 404 });
  });
}

async function section(assign = vi.fn()) {
  renderScreen(<BillingSection assign={assign} />, profile("writer"));
  return within(await screen.findByRole("region", { name: "Billing" }));
}

test("Settings shows what was granted, bought and spent, and what is left", async () => {
  serve(BALANCE);
  const region = await section();
  expect(region.getByText("Granted").nextSibling?.textContent).toBe("$500.00");
  expect(region.getByText("Purchased").nextSibling?.textContent).toBe("$25.00");
  expect(region.getByText("Spent").nextSibling?.textContent).toBe("$26.80");
  expect(region.getByText("Left").nextSibling?.textContent).toBe("$498.20");
  expect(region.queryByText("Grace")).toBeNull();
});

test("a user in grace sees the last day of it", async () => {
  serve({ ...BALANCE, grace_until: "2026-12-31", reason: "No limit until 2026-12-31." });
  const region = await section();
  expect(region.getByText("Grace").nextSibling?.textContent).toBe("until 2026-12-31");
});

test("a user on their own login has no balance, only what they spent", async () => {
  serve({ ...BALANCE, usd: 0, spent_usd: 0.12, own_login: true, reason: "Chat runs on your own ChatGPT login." });
  const region = await section();
  expect(region.getByText("Left").nextSibling?.textContent).toBe("--");
  expect(region.getByText("Spent").nextSibling?.textContent).toBe("$0.12");
});

test("a site with no ledger has no billing section", async () => {
  const sent = serve(null);
  renderScreen(<BillingSection assign={vi.fn()} />, profile("writer"));
  await waitFor(() => expect(sent.some((each) => each.path === "/api/addons/billing/balance")).toBe(true));
  expect(screen.queryByRole("region", { name: "Billing" })).toBeNull();
});

test("a top-up sends the user to the checkout page the server returned", async () => {
  const sent = serve(BALANCE);
  const assign = vi.fn();
  const region = await section(assign);
  fireEvent.click(region.getByRole("button", { name: "Add $25" }));
  await waitFor(() => expect(assign).toHaveBeenCalledWith("https://checkout.stripe.test/pay/cs_1"));
  expect(sent.filter((each) => each.method === "POST")).toMatchObject([{ path: "/api/addons/billing/topup", body: { usd: 25 } }]);
});

test("a top-up the server cannot start says so and goes nowhere", async () => {
  serve(BALANCE, () => Response.json({ detail: "Payments are not set up." }, { status: 503 }));
  const assign = vi.fn();
  const region = await section(assign);
  fireEvent.click(region.getByRole("button", { name: "Add $10" }));
  expect((await region.findByRole("alert")).textContent).toBe("Payments are not set up.");
  expect(assign).not.toHaveBeenCalled();
});
