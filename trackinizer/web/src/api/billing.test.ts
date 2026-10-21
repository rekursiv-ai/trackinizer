import { afterEach, expect, test, vi } from "vitest";
import { getBalance } from "./billing";
import { stubFetch } from "./testing";

afterEach(() => vi.unstubAllGlobals());

const BALANCE = { usd: 3.5, granted_usd: 10, purchased_usd: 0, spent_usd: 6.5, topup_usd: [10, 25, 50, 100] };

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

test("a spent balance is returned as spent, for the caller to grey the assistant", async () => {
  stubFetch(() => Response.json({ ...BALANCE, usd: 0 }));
  expect((await getBalance())?.usd).toBe(0);
});

test("a failure other than a missing ledger is raised, not read as a balance", async () => {
  stubFetch(() => Response.json({ detail: "boom" }, { status: 500 }));
  await expect(getBalance()).rejects.toMatchObject({ status: 500 });
});
