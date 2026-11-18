import { afterEach, expect, test, vi } from "vitest";
import { type BalanceBody, creditLine, getBalance, lowCredit, topUp } from "./billing";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

const VERDICT = { enforced: true, grace_until: null, own_login: false, can_run: true, reason: "You have credit left." };
const BALANCE: BalanceBody = { usd: 3.5, granted_usd: 10, purchased_usd: 0, spent_usd: 6.5, topup_usd: [10, 25, 50, 100], ...VERDICT };

test("the caller's balance is read from the billing addon's route", async () => {
  const sent = stubFetch(() => Response.json(BALANCE));
  expect(await getBalance()).toEqual(BALANCE);
  expect(sent).toMatchObject([{ method: "GET", path: "/api/addons/billing/balance" }]);
});

test("a site with no ledger answers 404, which reads as no balance at all", async () => {
  stubFetch(() => Response.json({ detail: "Not Found" }, { status: 404 }));
  expect(await getBalance()).toBeNull();
});

test("a balance without a number is not one: it reads as no ledger", async () => {
  stubFetch(() => Response.json({ detail: "not a ledger" }));
  expect(await getBalance()).toBeNull();
});

test("a balance without the server's verdict is not one either: no rule of the page's own stands in for it", async () => {
  const { can_run: _, ...older } = BALANCE;
  stubFetch(() => Response.json(older));
  expect(await getBalance()).toBeNull();
});

test("a spent balance is returned as the server judged it, for the caller to grey the assistant", async () => {
  stubFetch(() => Response.json({ ...BALANCE, usd: 0, can_run: false, reason: "You are out of credit." }));
  expect(await getBalance()).toMatchObject({ usd: 0, can_run: false, reason: "You are out of credit." });
});

test("a failure other than a missing ledger is raised, not read as a balance", async () => {
  stubFetch(() => Response.json({ detail: "boom" }, { status: 500 }));
  await expect(getBalance()).rejects.toMatchObject({ status: 500 });
});

test("the card says what is left and what was spent, in dollars and cents", () => {
  expect(creditLine({ ...BALANCE, usd: 498.2, spent_usd: 1.8 })).toEqual({ left: "$498.20 left", spent: "spent $1.80" });
  expect(creditLine({ ...BALANCE, usd: 0, spent_usd: 500 })).toEqual({ left: "$0.00 left", spent: "spent $500.00" });
});

test("a user in grace who spent past the grant sees what they owe as a negative", () => {
  const grace = { ...BALANCE, usd: -20, spent_usd: 520, grace_until: "2026-12-31" };
  expect(creditLine(grace)).toEqual({ left: "-$20.00 left", spent: "spent $520.00" });
});

test("a user on their own login has no balance to show, but still sees what they spent", () => {
  expect(creditLine({ ...BALANCE, usd: 0, spent_usd: 0.12, own_login: true })).toEqual({ left: "--", spent: "spent $0.12" });
});

test("the low-balance notice shows under $5 for a user whose credit decides, and for no one else", () => {
  const low = { ...BALANCE, usd: 4.2 };
  expect(lowCredit(low)).toBe("Low credit: $4.20 left.");
  expect(lowCredit({ ...low, usd: 5 })).toBeNull();
  expect(lowCredit({ ...low, grace_until: "2026-12-31" })).toBeNull();
  expect(lowCredit({ ...low, own_login: true })).toBeNull();
  expect(lowCredit({ ...low, enforced: false })).toBeNull();
  // Out of credit is the hard stop, which has its own words.
  expect(lowCredit({ ...low, usd: 0, can_run: false })).toBeNull();
});

test("a top-up asks the billing addon for a checkout and returns its page", async () => {
  const sent = stubFetch(() => Response.json({ url: "https://checkout.stripe.test/pay/cs_1" }));
  expect(await topUp(25)).toBe("https://checkout.stripe.test/pay/cs_1");
  expect(sent).toMatchObject([{ method: "POST", path: "/api/addons/billing/topup", body: { usd: 25 } }]);
});
