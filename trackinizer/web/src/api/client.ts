import createClient from "openapi-fetch";
import { type Fields, log } from "../debug/log";
import type { paths } from "./generated/schema";
import { newUuid } from "./idempotency";

/** How long a request may take before it fails as a timeout, by kind of request. */
export const TIMEOUT_MS = { read: 15_000, search: 6_000, write: 30_000 } as const;

/** Options every API function takes. */
export type CallOptions = {
  /** Aborts the request, for example when the view that asked for it goes away. */
  signal?: AbortSignal;
};

/**
 * The one typed client the API modules call through. Nothing else calls `fetch`.
 *
 * `tsc` checks every call's path, verb, parameters and body against the server's
 * schema (`openapi.json`, dumped by `scripts/openapi_dump.py`).
 */
export const client = createClient<paths>({
  // Absolute, because a Request outside a browser (in tests) has no page URL to
  // resolve a relative path against.
  baseUrl: globalThis.location.origin,
  // Looked up per call, not captured at creation, so tests can stub `fetch`.
  fetch: (request) => globalThis.fetch(request),
});

client.use({
  // Every request carries its own id, which the server echoes and logs beside
  // its line for the request, so a failure's id finds that line. The middleware
  // hook is openapi-fetch's documented way to add a header to every call.
  onRequest({ request }) {
    request.headers.set("X-Request-ID", newUuid());
  },
  // `send` sees only the response or the error, so each is mapped to its
  // request, for the failure's id, method and path.
  onResponse({ request, response, options }) {
    requests.set(response, request);
    // openapi-fetch parses a success's body inside the call, so a body that is
    // not JSON (a proxy's or a login page's HTML under a 200) would reject the
    // call like a dropped connection: `send` would say the server could not be
    // reached, and retry a write the server had already made. Refused here,
    // before the parse, it is the answer it was, under its own status. A call
    // that asks for the body unparsed (`parseAs`) takes whatever comes.
    const type = response.headers.get("content-type") ?? "";
    if (!response.ok || options.parseAs !== "json" || !hasBody(response)) return;
    if (/\bjson\b/i.test(type)) return readAsAnswer(response, request);
    const what = type.split(";")[0]!.trim() || "no content type";
    const error = new ApiError(response.status, `The server answered ${response.status} with ${what}, not JSON.`, "unreadable");
    requests.set(error, request);
    throw error;
  },
  onError({ request, error }) {
    if (typeof error === "object" && error !== null) requests.set(error, request);
  },
});

/** A request that failed, as `send` made it. */
export type SentRequest = {
  readonly method: string;
  /** The URL's path; never its query, which can hold search text or filter values. */
  readonly path: string;
  /** The `X-Request-ID` it carried; the server logs it beside the request's line. */
  readonly id: string;
  /** When it failed, as an ISO time. */
  readonly at: string;
  readonly ms: number;
  /**
   * How many times in a row this request has failed, counting this time: the
   * same method, URL and idempotency key. A read or a write retried with its
   * key counts up; a success starts it over.
   */
  readonly attempt: number;
};

/**
 * A failed request: the status and the server's message, and the request, when
 * `send` made it.
 *
 * `status` is 0 when no response came: `code` is then `"timeout"` or `"network"`.
 * A success whose body is not JSON keeps its status, with `code` `"unreadable"`.
 */
export class ApiError extends Error {
  readonly status: number;
  /** The server's `detail`, with a 422's field errors one per line. */
  readonly detail: string;
  /** The server's machine-readable `code`, when it sent one. */
  readonly code: string | null;
  readonly sent: SentRequest | null;

  constructor(status: number, detail: string, code: string | null = null, sent: SentRequest | null = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = code;
    this.sent = sent;
  }
}

/** A failure's facts as log fields: status, code, request, id and attempt; never the message. */
export function failureFields({ status, code, sent }: ApiError): Fields {
  return {
    method: sent?.method,
    path: sent?.path,
    status,
    code,
    ms: sent?.ms,
    request_id: sent?.id,
    attempt: sent?.attempt,
  };
}

/** What an openapi-fetch call resolves to. */
type Result<T> = { data?: T; error?: unknown; response: Response };

/**
 * Run one request under a timeout and the caller's signal; return its data.
 *
 * An error response, a timeout and a network failure all throw an `ApiError`,
 * carrying the request, and log one warning. A request the caller aborted
 * rejects with the caller's abort reason, as `fetch` does, so a cancelled query
 * is not reported as a failure. A success is logged at debug.
 */
export async function send<T>(
  timeoutMs: number,
  signal: AbortSignal | undefined,
  request: (signal: AbortSignal) => Promise<Result<T>>,
): Promise<T> {
  // A timer, not `AbortSignal.timeout`, which fake timers in tests cannot advance.
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), timeoutMs);
  const started = performance.now();
  let result: Result<T>;
  try {
    result = await request(
      signal ? AbortSignal.any([signal, timeout.signal]) : timeout.signal,
    );
  } catch (error) {
    if (signal?.aborted) throw error;
    throw failed(error instanceof ApiError ? error : noAnswer(error, timeout.signal.aborted, timeoutMs), requestOf(error), started);
  } finally {
    clearTimeout(timer);
  }
  const sent = requestOf(result.response);
  if (!result.response.ok && !redirectNotFollowed(result.response)) throw failed(apiError(result.response, result.error), sent, started);
  if (sent) {
    failing.delete(attemptKey(sent));
    log("debug", "request", { method: sent.method, path: pathOf(sent), status: result.response.status, ms: sinceMs(started), request_id: idOf(sent) });
  }
  // openapi-fetch leaves `data` undefined only for a body-less success (204).
  return result.data as T;
}

/**
 * A redirect its request chose not to follow (`redirect: "manual"`), which is
 * the answer: a browser gives it as an opaque redirect, with status 0, and Node
 * as the 3xx itself.
 */
function redirectNotFollowed(response: Response): boolean {
  return response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400);
}

/** A request that got no answer: it timed out, or the network failed. */
function noAnswer(error: unknown, timedOut: boolean, timeoutMs: number): ApiError {
  if (timedOut) return new ApiError(0, `No response within ${timeoutMs / 1000} s.`, "timeout");
  const reason = error instanceof Error ? error.message : String(error);
  return new ApiError(0, `The server could not be reached: ${reason}`, "network");
}

/**
 * `error` with the request that failed, logged as one warning. Without the
 * request (the client failed before sending one) it is logged as it is.
 */
function failed(error: ApiError, request: Request | undefined, started: number): ApiError {
  const failure = request
    ? new ApiError(error.status, error.detail, error.code, {
        method: request.method,
        path: pathOf(request),
        id: idOf(request),
        at: new Date().toISOString(),
        ms: sinceMs(started),
        attempt: countFailure(attemptKey(request)),
      })
    : error;
  log("warn", "request.failed", { ...failureFields(failure), ...(failure.status === 0 && { detail: failure.detail }) });
  return failure;
}

/** One more failure of the request `key` names, in a row; returns how many that makes. */
function countFailure(key: string): number {
  const count = (failing.get(key) ?? 0) + 1;
  // Re-inserted, so the oldest entry is first when the map is trimmed.
  failing.delete(key);
  failing.set(key, count);
  if (failing.size > MAX_FAILING) failing.delete(failing.keys().next().value!);
  return count;
}

/** What makes two sends the same request: method, URL and idempotency key. */
function attemptKey(request: Request): string {
  return `${request.method} ${request.url} ${request.headers.get("Idempotency-Key") ?? ""}`;
}

function pathOf(request: Request): string {
  return new URL(request.url).pathname;
}

function idOf(request: Request): string {
  return request.headers.get("X-Request-ID") ?? "";
}

function sinceMs(started: number): number {
  return Math.round(performance.now() - started);
}

/**
 * The server's error body as an `ApiError`.
 *
 * Domain errors are `{detail: "<text>", code}`; request validation (422) is
 * `{detail: [{loc, msg, type}, …]}`. Anything else, such as a proxy's HTML error
 * page, falls back to the status line.
 */
function apiError(response: Response, body: unknown): ApiError {
  const { detail, code } = fieldsOf(body);
  const text =
    typeof detail === "string"
      ? detail
      : Array.isArray(detail)
        ? detail.map(fieldError).join("\n")
        : `${response.status} ${response.statusText}`.trim();
  return new ApiError(response.status, text, typeof code === "string" ? code : null);
}

/** One FastAPI validation error as `field: message`; the `body` prefix is dropped. */
function fieldError(error: unknown): string {
  const { loc, msg } = fieldsOf(error);
  const where = Array.isArray(loc) ? loc.filter((part) => part !== "body").join(".") : "";
  return where ? `${where}: ${String(msg)}` : String(msg);
}

/** Whether `response` has a body to parse, as openapi-fetch decides it. */
function hasBody(response: Response): boolean {
  return response.status !== 204 && response.headers.get("content-length") !== "0";
}

/**
 * Make a JSON success that does not parse the server's unreadable answer, under its
 * status and carrying its request, as a non-JSON body is. openapi-fetch parses it
 * after `onResponse`, with `response.json()` when the length is known and with
 * `JSON.parse` of `response.text()` otherwise, and its parse error would reach
 * `send` bare, read as a dropped connection and retried. Without a length, the text
 * is parsed here once more; the server sends a length with every JSON answer.
 */
function readAsAnswer(response: Response, request: Request): void {
  const unreadable = () => {
    const error = new ApiError(response.status, `The server answered ${response.status} with JSON it could not read.`, "unreadable");
    requests.set(error, request);
    return error;
  };
  const json = response.json.bind(response);
  const text = response.text.bind(response);
  response.json = async () => {
    try {
      return await json();
    } catch {
      throw unreadable();
    }
  };
  response.text = async () => {
    const raw = await text();
    try {
      if (raw) JSON.parse(raw);
    } catch {
      throw unreadable();
    }
    return raw;
  };
}

/** A parsed JSON value's properties; none for a string, a number or `null`. */
function fieldsOf(value: unknown): { [key: string]: unknown } {
  return typeof value === "object" && value !== null ? { ...value } : {};
}

/** The request `value`, a response or an error, came from, when the client sent one. */
function requestOf(value: unknown): Request | undefined {
  return typeof value === "object" && value !== null ? requests.get(value) : undefined;
}

/** Each response and error the client passed on, to the request it answered. */
const requests = new WeakMap<object, Request>();

/**
 * Requests failing now, to how many times in a row. A request that fails and is
 * never sent again stays, so the map keeps only the latest few.
 */
const failing = new Map<string, number>();
const MAX_FAILING = 100;
