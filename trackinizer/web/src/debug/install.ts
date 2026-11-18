import type { ErrorInfo } from "react";
import { ApiError, failureFields } from "../api/client";
import { errorDetails, refusedDetails } from "./details";
import { type Fields, log, recentEvents, setDebug } from "./log";
import { recentTimings } from "./timings";

/**
 * Start debug support, once, before the app renders.
 *
 * - Debug is on with `?debug=1` in the page's address, or while `localStorage`
 *   holds `trackinizer.v2.debug` = `1`; info and debug events then reach the
 *   console too.
 * - Uncaught errors and unhandled rejections are logged.
 * - The devtools console gets `trackinizer.events()`, the ring,
 *   `trackinizer.details()`, the text Copy details would give, or the text
 *   the clipboard last refused it, and `trackinizer.timings()`, the canvas's
 *   frame and paint marks.
 *
 * Returns the function that undoes it.
 */
export function installDebug(): () => void {
  setDebug(debugAsked());
  const uncaught = (event: ErrorEvent) => log("error", "uncaught", errorFields(event.error ?? event.message));
  const unhandled = (event: PromiseRejectionEvent) => log("error", "unhandled_rejection", errorFields(event.reason));
  addEventListener("error", uncaught);
  addEventListener("unhandledrejection", unhandled);
  const details = () => refusedDetails() ?? errorDetails("Asked for in the console", null);
  Object.assign(window, { trackinizer: { events: recentEvents, details, timings: recentTimings } });
  return () => {
    removeEventListener("error", uncaught);
    removeEventListener("unhandledrejection", unhandled);
    Reflect.deleteProperty(window, "trackinizer");
    setDebug(false);
  };
}

/**
 * Log a render crash; React's root calls it for every error a render throws
 * (`onCaughtError`, `onUncaughtError`), in place of its own console output.
 */
export function logRenderError(error: unknown, info: ErrorInfo): void {
  log("error", "render.crash", { ...errorFields(error), component_stack: info.componentStack });
}

/** Whether the page asks for debug: `?debug=1`, or the `localStorage` switch. */
function debugAsked(): boolean {
  if (new URLSearchParams(location.search).get("debug") === "1") return true;
  try {
    return localStorage.getItem(DEBUG_KEY) === "1";
  } catch {
    // Storage off: debug stays off unless the address asks.
    return false;
  }
}

/** What an error says about itself; a request's failure without the server's text. */
function errorFields(error: unknown): Fields {
  if (error instanceof ApiError) return { error: error.name, ...failureFields(error) };
  if (error instanceof Error) return { error: error.name, message: error.message, stack: error.stack };
  return { error: String(error) };
}

const DEBUG_KEY = "trackinizer.v2.debug";
