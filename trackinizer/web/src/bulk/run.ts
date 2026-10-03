import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { FieldWrite, InquiryRow } from "../api/inquiries";
import { bootQueries } from "../app/boot";
import { useToast } from "../ui/toast";
import { type Edit, type Guard, sameValue } from "../writes/edits";
import { isTransient, sendWithRetries, type WriteRequest } from "../writes/requests";
import { refetchShowing } from "../writes/useWrite";
import type { BulkChange } from "./edits";

/**
 * Where one row's write is.
 *
 * - `pending`: waiting to be sent, or sending.
 * - `done`: it landed.
 * - `unchanged`: the row had the value already, as the server said, or as it
 *   was found when someone else had set it.
 * - `failed`: no answer, or a 5xx, after the automatic retries. A retry reads
 *   the row first, and sends the same request again only if it has not landed.
 * - `conflict`: someone changed the value after the list loaded it, as the
 *   check before the write, a compare-and-set 409, or a resend's read found. A
 *   retry saves this value over theirs.
 * - `rejected`: the server refused it otherwise (400, 403, 404, 422, or a 409
 *   with the value unmoved); asking again gets the same answer.
 */
export type Outcome =
  | { readonly status: "pending" | "done" | "unchanged" }
  | { readonly status: "failed" | "rejected"; readonly message: string }
  | { readonly status: "conflict"; readonly message: string; readonly theirs: unknown };

/** One row of a bulk edit: the row, its edit, and where it is. */
export type Entry = { readonly row: InquiryRow; readonly edit: Edit<FieldWrite>; readonly outcome: Outcome };

/** A bulk edit's rows, each with its outcome; `running` while any is being sent. */
export type BulkReport = { readonly title: string; readonly entries: readonly Entry[]; readonly running: boolean };

/** How a bulk edit sends one write, with its retries. */
export type Io = { readonly send: (request: WriteRequest<FieldWrite>) => Promise<FieldWrite> };

/**
 * Apply one change to many rows, one request per row, each with its own key.
 *
 * `start` sends them; `report` then holds each row's outcome until every row
 * has landed, when a toast says so instead. `retry` sends only the rows that
 * failed or met a conflict. A `start` while a report shows, running or waiting
 * to be retried or closed, is ignored, as is a `retry` while one runs: a new
 * edit would replace the report, and the retry it offers with it. There is no
 * undo: the plan offers one only where the inverse is exactly one write.
 */
export function useBulkEdit() {
  const queryClient = useQueryClient();
  const toast = useToast();
  // Through the mutation cache, so a 401 ends the session as it does for any write.
  const { mutateAsync } = useMutation({
    mutationFn: (request: WriteRequest<FieldWrite>) => sendWithRetries(request),
    networkMode: "always",
  });
  const [report, setReport] = useState<BulkReport | null>(null);
  const latest = useRef<BulkReport | null>(null);
  const publish = useCallback((next: BulkReport | null) => {
    latest.current = next;
    setReport(next);
  }, []);

  const run = useCallback(
    async (title: string, entries: readonly Entry[]) => {
      const io: Io = {
        send: async (request) => {
          try {
            return await mutateAsync(request);
          } catch (error) {
            // The role may have changed; the profile says what it is now.
            if (error instanceof ApiError && error.status === 403) {
              void queryClient.invalidateQueries({ queryKey: bootQueries.profile.queryKey });
            }
            throw error;
          }
        },
      };
      const settled = [...entries];
      publish({ title, entries, running: true });
      await sendPending(entries, io, (index, entry) => {
        settled[index] = entry;
        publish({ title, entries: [...settled], running: true });
      });
      await refetchShowing(
        queryClient,
        entries.flatMap((entry) => entry.edit.touches),
      );
      if (settled.every(landed)) {
        publish(null);
        toast(summary(title, settled));
      } else {
        publish({ title, entries: settled, running: false });
      }
    },
    [mutateAsync, publish, queryClient, toast],
  );

  return {
    report,
    /** Apply `change` to `rows`. */
    start: (change: BulkChange, rows: readonly InquiryRow[]) => {
      if (latest.current) return;
      void run(
        change.title,
        rows.map((row) => ({ row, edit: change.edit(row), outcome: PENDING })),
      );
    },
    /** Send again only the rows that failed or met a conflict. */
    retry: () => {
      const shown = latest.current;
      if (shown && !shown.running) void run(shown.title, forRetry(shown.entries));
    },
    dismiss: () => {
      if (!latest.current?.running) publish(null);
    },
  };
}

/** How many rows' writes are sent at once. */
export const CONCURRENT = 4;

/**
 * Send every pending entry's edit, `CONCURRENT` at a time, and hand each entry
 * with its outcome to `onSettled` as it settles.
 */
export async function sendPending(
  entries: readonly Entry[],
  io: Io,
  onSettled: (index: number, entry: Entry) => void,
): Promise<void> {
  const queue = entries.flatMap((entry, index) => (entry.outcome.status === "pending" ? [index] : []));
  const worker = async () => {
    for (let index = queue.shift(); index !== undefined; index = queue.shift()) {
      const entry = entries[index]!;
      onSettled(index, { ...entry, outcome: await settle(entry.edit, io) });
    }
  };
  await Promise.all(Array.from({ length: CONCURRENT }, worker));
}

/**
 * Send one row's edit, honouring its guard as a single edit does (`useWrite`),
 * and say how it went. A check reads the row first: a value someone changed
 * since the list loaded it is a conflict, and nothing is sent. After a 409, a
 * compare-and-set's or a resend's read finding someone else's value, read the
 * row: a value that did not move was refused for another reason, and one that
 * moved is a conflict. A value that moved to this very one, or a write the
 * server says changed nothing, is unchanged.
 */
export async function settle(edit: Edit<FieldWrite>, io: Io): Promise<Outcome> {
  const guard = edit.guard;
  try {
    if (guard?.type === "check") {
      const stored = await guard.read();
      if (!sameValue(stored, guard.base)) return moved(guard, stored);
    }
    return (await io.send(edit.request)).change_id === null ? UNCHANGED : DONE;
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return guard && error.status === 409 ? refused(guard, error) : failure(error);
  }
}

/**
 * The entries a retry sends: failed ones again, pending, with the same request,
 * which reads first whether it landed; conflicts as a fresh edit, with a
 * fresh key, guarded by the value now stored. The rest stay as they are.
 */
export function forRetry(entries: readonly Entry[]): Entry[] {
  return entries.map((entry) => {
    const { edit, outcome } = entry;
    if (outcome.status === "failed") return { ...entry, outcome: PENDING };
    if (outcome.status === "conflict" && edit.guard) return { ...entry, edit: edit.guard.again(outcome.theirs), outcome: PENDING };
    return entry;
  });
}

/** Whether the entry's row has the value now: saved, or already so. */
export function landed(entry: Entry): boolean {
  return entry.outcome.status === "done" || entry.outcome.status === "unchanged";
}

/** How many rows a retry would send. */
export function retryCount(entries: readonly Entry[]): number {
  return entries.filter((entry) => entry.outcome.status === "failed" || entry.outcome.status === "conflict").length;
}

/** The toast once every row has landed: `Status set to Complete on 3 inquiries; 1 was already so.` */
export function summary(title: string, entries: readonly Entry[]): string {
  const done = entries.filter((entry) => entry.outcome.status === "done").length;
  const unchanged = entries.length - done;
  if (done === 0) return `${title}: ${entries.length === 1 ? "it was" : `all ${entries.length} were`} already so.`;
  const already = unchanged ? `; ${unchanged} ${unchanged === 1 ? "was" : "were"} already so` : "";
  return `${title} on ${done} ${done === 1 ? "inquiry" : "inquiries"}${already}.`;
}

const PENDING: Outcome = { status: "pending" };
const DONE: Outcome = { status: "done" };
const UNCHANGED: Outcome = { status: "unchanged" };

/** After a compare-and-set 409, `refusal`: read the row to say why. */
async function refused(guard: Guard<FieldWrite>, refusal: ApiError): Promise<Outcome> {
  let theirs: unknown;
  try {
    theirs = await guard.read();
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    return failure(error);
  }
  return sameValue(theirs, guard.base) ? { status: "rejected", message: failureMessage(refusal) } : moved(guard, theirs);
}

/** A row whose value someone changed to `theirs` since the list loaded it. */
function moved(guard: Guard<FieldWrite>, theirs: unknown): Outcome {
  if (sameValue(theirs, guard.mine)) return UNCHANGED;
  return { status: "conflict", theirs, message: `Changed to ${guard.show(theirs)} since the list loaded it. Retry saves yours over it.` };
}

function failure(error: ApiError): Outcome {
  return { status: isTransient(error) ? "failed" : "rejected", message: failureMessage(error) };
}

/**
 * A failure in the write layer's words (`useWrite`), for the row's line in the
 * report. A 401 needs none: it ends the session.
 */
function failureMessage(error: ApiError): string {
  if (error.status === 0) return `Not saved. ${error.detail}`;
  if (error.status === 403) return `Your role cannot make this change. ${error.detail}`;
  return error.status >= 500 ? `Not saved: the server failed (${error.detail}).` : error.detail;
}
