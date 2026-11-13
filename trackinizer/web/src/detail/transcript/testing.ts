// Test helpers for the transcript; only tests import this file.

type Fields = { readonly [field: string]: unknown };

/** The fields of a `ToolResult` payload besides `result`; one not given is left out, as the server leaves a default out. */
type CallFields = {
  readonly call_id?: string;
  readonly state?: "pending" | "done" | "failed" | "cancelled";
  /** The items the tool wrote: text, and typed items, each tagged with its class. */
  readonly stdout?: readonly unknown[];
  readonly stderr?: readonly unknown[];
  readonly shown?: string;
  readonly extra?: Fields;
};

/** An object tagged with its class `tag`, as the server's codec writes one: a tool's result, such as `tagged("ReadResult", { path })`. */
export function tagged(tag: string, fields: Fields = {}): Fields {
  return { "py/object": tag, ...fields };
}

/**
 * A `ToolResult` payload in the shape the server returns (`served_payload` in
 * `types/session_records.py`): the tool's `result` (null when the tool returned
 * none) and the call's other fields. The record's kind is not in the payload; a
 * test passes the kind the row is stored under (`record_kind` there): the
 * result's class, else `ToolErrorResult` for a failed call,
 * `UncategorizedToolResult` for a done one, and `ToolResult` for a pending or
 * cancelled one.
 */
export function toolResult(result: Fields | null, { call_id = "", state = "done", stdout = [], stderr = [], shown, extra }: CallFields = {}): Fields {
  return {
    "py/object": "ToolResult",
    call_id,
    state,
    ...(stdout.length ? { stdout } : {}),
    ...(stderr.length ? { stderr } : {}),
    ...(result === null ? {} : { result }),
    ...(shown === undefined ? {} : { shown }),
    ...(extra === undefined ? {} : { extra }),
  };
}

/**
 * An Edit call's payload: `stdout` is the line "Edited <path>." and then each of
 * `splices` as a `Splice`; `path` is null when the provider named no file.
 */
export function editResult(path: string | null, splices: readonly Fields[], call: Omit<CallFields, "stdout"> = {}): Fields {
  return toolResult(tagged("EditResult", path === null ? {} : { path }), {
    ...call,
    stdout: [`Edited ${path ?? "the file"}.`, ...splices.map((splice) => ({ "py/object": "Splice", ...splice }))],
  });
}

/** A WebSearch call's payload: `stdout` is the hits as the text the model read, and then each hit as a `WebSearchHit`. */
export function searchResult(fields: Fields, hits: readonly { readonly [field: string]: string }[], call: Omit<CallFields, "stdout"> = {}): Fields {
  const text = hits.map((hit) => [hit.title, hit.url, hit.snippet].filter((part) => part !== undefined).join("\n")).join("\n\n");
  return toolResult(tagged("WebSearchResult", fields), {
    ...call,
    stdout: [...(text ? [text] : []), ...hits.map((hit) => ({ "py/object": "WebSearchHit", ...hit }))],
  });
}

/** A Bash call's payload: the text on each stream, and the command and exit code in its `BashResult`. */
export function shellResult(
  { command, stdout = "", stderr = "", exit }: { readonly command?: readonly string[] | null; readonly stdout?: string; readonly stderr?: string; readonly exit?: number | null },
  call: Omit<CallFields, "stdout" | "stderr"> = {},
): Fields {
  return toolResult(tagged("BashResult", { ...(command ? { command } : {}), ...(typeof exit === "number" ? { exit_code: exit } : {}) }), {
    ...call,
    stdout: stdout ? [stdout] : [],
    stderr: stderr ? [stderr] : [],
  });
}
