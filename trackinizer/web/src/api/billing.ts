// The caller's credit, as a deployment's billing addon keeps it under /api/addons/billing. The addon's routes
// are not in openapi.json (the dump leaves every addon out), so the one read here is typed by hand and its
// answer is checked at runtime, as `private/openaiLogin/api.ts` does for its addon.
import createClient from "openapi-fetch";
import { ApiError, type CallOptions, send, TIMEOUT_MS } from "./client";

/** The caller's credit ledger, as the billing addon answers it (`BalanceBody`); `usd` is what is left. */
export type BalanceBody = {
  readonly usd: number;
  readonly granted_usd: number;
  readonly purchased_usd: number;
  readonly spent_usd: number;
  topup_usd: number[];
  readonly reversed_usd?: number;
};

type AddonPaths = {
  "/api/addons/billing/balance": {
    get: {
      responses: { 200: { content: { "application/json": BalanceBody } } };
    };
  };
};

const addon = createClient<AddonPaths>({
  baseUrl: globalThis.location.origin,
  fetch: (request) => globalThis.fetch(request),
});

/**
 * The caller's credit on this site's billing addon, or null when the site has no ledger (its route is
 * not mounted: a 404). A balance the server sends without a number is not one: it reads as no ledger too.
 */
export async function getBalance({ signal }: CallOptions = {}): Promise<BalanceBody | null> {
  try {
    const balance = await send(TIMEOUT_MS.read, signal, (signal) => addon.GET("/api/addons/billing/balance", { signal }));
    return typeof balance?.usd === "number" ? balance : null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}
