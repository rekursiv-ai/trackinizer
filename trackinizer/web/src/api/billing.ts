// The caller's credit, as a deployment's billing addon keeps it under /api/addons/billing. The addon's routes
// are not in openapi.json (the dump leaves every addon out), so the reads here are typed by hand and each answer
// is checked at runtime, as `private/openaiLogin/api.ts` does for its addon.
import { queryOptions } from "@tanstack/react-query";
import createClient from "openapi-fetch";
import { ApiError, type CallOptions, send, TIMEOUT_MS } from "./client";

/**
 * The caller's credit ledger and the server's verdict on it, as the billing addon answers (`BalanceBody`). `usd` is
 * what is left. The page keeps no rule of its own about who may run: `can_run` is the server's, and `reason` says why.
 */
export type BalanceBody = {
  readonly usd: number;
  readonly granted_usd: number;
  readonly purchased_usd: number;
  readonly spent_usd: number;
  topup_usd: number[];
  readonly reversed_usd?: number;
  /** Whether this site refuses a user whose credit is spent. */
  readonly enforced: boolean;
  /** The last day of the caller's grace (`2026-12-31`), or null when they have none or it ended. */
  readonly grace_until: string | null;
  /** Whether the caller's Chat turns run on their own ChatGPT login. */
  readonly own_login: boolean;
  /** Whether the caller may take a Chat turn now (a launch the house pays for is judged apart, by `can_launch`). */
  readonly can_run: boolean;
  /** One plain sentence for why. */
  readonly reason: string;
};

/** Below this many dollars left, a user whose credit decides is told before the hard stop. */
const LOW_CREDIT_USD = 5;

type AddonPaths = {
  "/api/addons/billing/balance": {
    get: {
      responses: { 200: { content: { "application/json": BalanceBody } } };
    };
  };
  "/api/addons/billing/topup": {
    post: {
      requestBody: { content: { "application/json": { usd: number } } };
      responses: { 200: { content: { "application/json": { url: string } } } };
    };
  };
};

const addon = createClient<AddonPaths>({
  baseUrl: globalThis.location.origin,
  fetch: (request) => globalThis.fetch(request),
});

/**
 * The caller's credit on this site's billing addon, or null when the site has no ledger (its route is
 * not mounted: a 404). A balance the server sends without a number, or without its verdict, is not one: it reads as
 * no ledger too.
 */
export async function getBalance({ signal }: CallOptions = {}): Promise<BalanceBody | null> {
  try {
    const balance = await send(TIMEOUT_MS.read, signal, (signal) => addon.GET("/api/addons/billing/balance", { signal }));
    return typeof balance?.usd === "number" && typeof balance.can_run === "boolean" ? balance : null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

/** The caller's balance as TanStack Query options, read at most once a minute; the card and Settings share it. */
export const balanceQuery = queryOptions({
  queryKey: ["billing", "balance"],
  queryFn: ({ signal }) => getBalance({ signal }),
  staleTime: 60_000,
  retry: false,
});

/** Start a Stripe Checkout for `usd`, one of the balance's `topup_usd`; returns the page to pay on. */
export async function topUp(usd: number): Promise<string> {
  const answer = await send(TIMEOUT_MS.write, undefined, (signal) => addon.POST("/api/addons/billing/topup", { body: { usd }, signal }));
  return answer.url;
}

/** What the card shows of the balance: what is left, or `--` where it is not the user's limit, and what was spent. */
export function creditLine(balance: BalanceBody): { readonly left: string; readonly spent: string } {
  return { left: balance.own_login ? "--" : `${dollars(balance.usd)} left`, spent: `spent ${dollars(balance.spent_usd)}` };
}

/**
 * The notice before the hard stop: shown only to a user whose credit alone decides whether they may run, once less
 * than $5 is left. A user in grace, on their own login, or where credit is not enforced has no stop to warn of.
 */
export function lowCredit(balance: BalanceBody): string | null {
  const metered = balance.enforced && balance.grace_until === null && !balance.own_login;
  return metered && balance.can_run && balance.usd < LOW_CREDIT_USD ? `Low credit: ${dollars(balance.usd)} left.` : null;
}

/** Dollars and cents, with the minus sign before the dollar sign. */
export function dollars(usd: number): string {
  return `${usd < 0 ? "-" : ""}$${Math.abs(usd).toFixed(2)}`;
}
