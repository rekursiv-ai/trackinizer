import { type QueryClient, useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { bootQueries } from "../app/boot";
import { log } from "../debug/log";
import { detailQueries } from "../detail/queries";
import { LiveContext } from "../live/index";
import { type ShowToast, useToast } from "../ui/toast";
import { type Edit, fieldValue, type Guard, lastChangeOf, sameValue } from "./edits";
import { foundLanded, isTransient, sendWithRetries, type WriteRequest } from "./requests";

/**
 * Where one control's write is.
 *
 * - `pending`: sending, retrying, or refetching what it changed. The control
 *   shows it and takes no second edit.
 * - `failed`: no answer, or a 5xx, after the automatic retries (none on an
 *   account write, which is never resent on its own). The edit stays, with
 *   Retry and Discard.
 * - `rejected`: the server refused it (400, 403, 404, 409, 422), or an edit sent
 *   once only got no sure answer (`maybeLanded`); its message goes next to the
 *   control, one line per field for a 422. Both keep the error, for Copy details.
 * - `conflict`: the stored value is not the one editing began from. `who` changed
 *   it, when the change log says (after a 409); the user keeps theirs or saves
 *   mine over it.
 */
export type WriteState =
  | { readonly status: "idle" }
  | { readonly status: "pending" }
  | { readonly status: "failed"; readonly message: string; readonly error: ApiError; readonly retry: () => void; readonly discard: () => void }
  | { readonly status: "rejected"; readonly message: string; readonly error: ApiError }
  | {
      readonly status: "conflict";
      readonly label: string;
      readonly who: string | null;
      readonly theirs: string;
      readonly mine: string;
      readonly saveMine: () => void;
      readonly keepTheirs: () => void;
    };

/** One control's writes: its state, and `run`, which sends an edit through to the end. */
export type Writer = {
  readonly state: WriteState;
  /**
   * Send `edit`: check or compare-and-set, send with retries, refetch what it
   * changed, then toast `done` with Undo. Resolves with the server's answer once
   * it landed, or null otherwise: discarded, refused, theirs kept, or maybe
   * landed. A second edit while one is under way is ignored and resolves null.
   *
   * Once the control is gone, the edit still goes through, and it resolves null
   * even when it lands: a flow the user closed takes no further step, and the
   * toast is what says how the edit ended. A choice it was showing (Retry, or
   * the conflict dialog) takes its default: the stored value stays.
   */
  readonly run: <Result>(edit: Edit<Result>) => Promise<Result | null>;
};

/**
 * Write through the one path every edit takes. Render `WriteStatus` with the
 * state next to the control; it holds the conflict dialog too.
 */
export function useWrite(): Writer {
  const queryClient = useQueryClient();
  const toast = useToast();
  const hub = useContext(LiveContext);
  // Through the mutation cache, so a 401 ends the session as it does for reads.
  // "always": the plan has no offline queue, so an offline send fails at once.
  const { mutateAsync } = useMutation({
    mutationFn: (request: WriteRequest<unknown>) => sendWithRetries(request),
    networkMode: "always",
  });
  const [state, setState] = useState<WriteState>(IDLE);
  const busy = useRef(false);
  const mounted = useRef(false);
  const gone = useRef<(() => void) | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      gone.current?.();
    };
  }, []);

  const context = useRef<Context | null>(null);
  const run = useCallback(async <Result,>(edit: Edit<Result>): Promise<Result | null> => {
    if (busy.current) return null;
    busy.current = true;
    try {
      const result = await runEdit(edit, context.current!);
      return mounted.current ? result : null;
    } finally {
      busy.current = false;
    }
  }, []);
  useLayoutEffect(() => {
    context.current = {
      queryClient,
      toast,
      // `mutateAsync` resolves with what `sendWithRetries(request)` returned.
      send: <Result,>(request: WriteRequest<Result>) => mutateAsync(request) as Promise<Result>,
      show: setState,
      changed: (ids) => {
        for (const id of ids) hub?.change(id);
      },
      mounted: () => mounted.current,
      whenGone: (answer) => {
        gone.current = answer;
      },
      run,
    };
  });
  return { state, run };
}

/**
 * Refetch every cached read that shows one of `ids`: one keyed by the id, or
 * whose data holds a row or neighbour with that id. Resolves once the reads on
 * screen are back, failed or not.
 */
export async function refetchShowing(queryClient: QueryClient, ids: readonly string[]): Promise<void> {
  const touched = new Set(ids);
  // Unlike the stream layer's refetches, this one cancels a read already in
  // flight: that read may have started before the write and would show it undone.
  await queryClient.invalidateQueries({
    predicate: (query) =>
      query.queryKey.some((part) => typeof part === "string" && touched.has(part)) || showsRow(query.state.data, touched),
  });
}

const IDLE: WriteState = { status: "idle" };
const PENDING: WriteState = { status: "pending" };

/** What the steps of a write need from the hook. */
type Context = {
  readonly queryClient: QueryClient;
  readonly toast: ShowToast;
  readonly send: <Result>(request: WriteRequest<Result>) => Promise<Result>;
  readonly show: (state: WriteState) => void;
  /** Hand the live hub rows a landed write changed or made, as the stream's ids. */
  readonly changed: (ids: readonly string[]) => void;
  /** Whether the control is still on screen to show a state or a dialog. */
  readonly mounted: () => boolean;
  /** Call `answer` if the control goes while a choice shows; null once it is answered. */
  readonly whenGone: (answer: (() => void) | null) => void;
  readonly run: <Result>(edit: Edit<Result>) => Promise<Result | null>;
};

async function runEdit<Result>(edit: Edit<Result>, context: Context): Promise<Result | null> {
  context.show(PENDING);
  const guard = edit.guard;
  if (guard?.type !== "check") return sendEdit(edit, context);
  let stored: unknown;
  try {
    stored = await guard.read();
  } catch (error) {
    return failed(error, edit.touches, context, () => runEdit(edit, context));
  }
  if (!sameValue(stored, guard.base) && !(await ask(guard, stored, null, context))) {
    await keepTheirs(edit.touches, context);
    return null;
  }
  context.show(PENDING);
  return sendEdit(edit, context);
}

/**
 * Send the edit's one request; a retry sends that same request again, once a
 * read finds it has not landed (`sendWithRetries`).
 */
async function sendEdit<Result>(edit: Edit<Result>, context: Context): Promise<Result | null> {
  let result: Result;
  try {
    result = await context.send(edit.request);
  } catch (error) {
    if (edit.guard && error instanceof ApiError && error.status === 409) return resolveConflict(edit, edit.guard, error, context);
    if (edit.maybeLanded !== undefined && isTransient(error)) return refused(edit.maybeLanded, error, context);
    return failed(error, edit.touches, context, () => sendEdit(edit, context));
  }
  await refetchShowing(context.queryClient, edit.touches);
  context.changed([...edit.touches, ...(edit.creates?.(result) ?? [])]);
  context.show(IDLE);
  if (edit.done) {
    const undo = foundLanded(result) ? undefined : edit.undo?.(result);
    context.toast(edit.done, undo ? { undo: () => void context.run(undo) } : {});
  }
  return result;
}

/**
 * After a 409, the server's compare-and-set or a resend's read finding someone
 * else's value: refetch the row, and show who changed the field to what. Saving
 * mine is a new edit, with a fresh key, expecting the value now stored. A field
 * still at `base` was refused for another reason (`conflict`), whose message
 * shows instead.
 */
async function resolveConflict<Result>(
  edit: Edit<Result>,
  guard: Guard<Result>,
  conflict: ApiError,
  context: Context,
): Promise<Result | null> {
  const read = detailQueries.detail(guard.id);
  let detail;
  try {
    // A read of the row already under way may have begun before the change
    // that refused this write; joining it would show the value it replaced.
    await context.queryClient.cancelQueries({ queryKey: read.queryKey });
    detail = await context.queryClient.fetchQuery({ ...read, staleTime: 0 });
  } catch (error) {
    return failed(error, edit.touches, context, () => resolveConflict(edit, guard, conflict, context));
  }
  const theirs = fieldValue(detail.self, guard.field);
  if (sameValue(theirs, guard.base)) return failed(conflict, edit.touches, context, () => sendEdit(edit, context));
  if (sameValue(theirs, guard.mine)) {
    await keepTheirs(edit.touches, context);
    return null;
  }
  const who = lastChangeOf(detail.changes, guard.field)?.actor ?? null;
  if (await ask(guard, theirs, who, context)) return runEdit(guard.again(theirs), context);
  await keepTheirs(edit.touches, context);
  return null;
}

/** Ask whether to save mine over `theirs`; resolves true to save. */
function ask<Result>(guard: Guard<Result>, theirs: unknown, who: string | null, context: Context): Promise<boolean> {
  const [label, shown] = [guard.label, guard.show(theirs)];
  // Neither value is logged: either can be text a user wrote.
  log("info", "write.conflict", { guard: guard.type, label, ...(guard.type === "cas" && { id: guard.id, field: guard.field }) });
  return choice(
    context,
    (choose) =>
      context.show({
        status: "conflict",
        label,
        who,
        theirs: shown,
        mine: guard.show(guard.mine),
        saveMine: () => choose(true),
        keepTheirs: () => choose(false),
      }),
    () => {
      context.toast(`Not saved: ${who ?? "someone"} changed ${label} to ${shown}`, { failed: true });
      return false;
    },
  );
}

/** Leave the stored value, and show it wherever the edit's rows are on screen. */
async function keepTheirs(touches: readonly string[], context: Context): Promise<void> {
  context.show(PENDING);
  await refetchShowing(context.queryClient, touches);
  context.show(IDLE);
}

/**
 * Show why the edit failed; resolves with what `retry` leads to when the user
 * asks to retry it, or null.
 *
 * Only a failure with no answer or a 5xx offers Retry; any other answer is the
 * server's, and sending the same request again gets the same one. A 403 refetches
 * the profile, since the role may have changed; a 404 refetches the edit's rows,
 * so a purged one shows as gone. `touches` are the edit's rows.
 *
 * A control that is gone shows no Retry: a toast offers it, and the write
 * resolves null meanwhile, as a discarded one does.
 */
async function failed<Result>(
  error: unknown,
  touches: readonly string[],
  context: Context,
  retry: () => Promise<Result | null>,
): Promise<Result | null> {
  if (!(error instanceof ApiError)) throw error;
  if (error.status === 403) void context.queryClient.invalidateQueries({ queryKey: bootQueries.profile.queryKey });
  if (error.status === 404) void refetchShowing(context.queryClient, touches);
  const message = failureMessage(error);
  if (!isTransient(error)) return refused(message, error, context);
  const again = await choice(
    context,
    (choose) => {
      const answer = (retrying: boolean) => {
        if (choose(retrying)) context.show(retrying ? PENDING : IDLE);
      };
      context.show({ status: "failed", message, error, retry: () => answer(true), discard: () => answer(false) });
    },
    () => {
      context.toast(message, { retry: () => void retry(), error });
      return false;
    },
  );
  return again ? retry() : null;
}

/** Show `message` next to the control, or in a toast once it is gone; no Retry follows. */
function refused(message: string, error: ApiError, context: Context): null {
  context.show({ status: "rejected", message, error });
  if (!context.mounted()) context.toast(message, { failed: true, error });
  return null;
}

function failureMessage(error: ApiError): string {
  switch (error.status) {
    case 0:
      return `Not saved. ${error.detail}`;
    case 401:
      return "Your session has ended. Sign in again to save.";
    case 403:
      return `Your role cannot make this change. ${error.detail}`;
  }
  return error.status >= 500 ? `Not saved: the server failed (${error.detail}).` : error.detail;
}

/** Whether `value` holds an object whose `id` is one of `ids`. */
function showsRow(value: unknown, ids: ReadonlySet<string>): boolean {
  if (typeof value !== "object" || value === null) return false;
  if (Array.isArray(value)) return value.some((item) => showsRow(item, ids));
  const record = value as { readonly [key: string]: unknown };
  return (typeof record.id === "string" && ids.has(record.id)) || Object.values(record).some((item) => showsRow(item, ids));
}

/**
 * Offer a choice beside the control; resolves with the first answer, since a
 * dialog's button and its close both answer (`choose` says whether it was the
 * first). A control that is gone, or goes while the choice shows, cannot be
 * answered, so `unanswered` gives the answer.
 */
function choice<Answer>(
  context: Context,
  offer: (choose: (answer: Answer) => boolean) => void,
  unanswered: () => Answer,
): Promise<Answer> {
  if (!context.mounted()) return Promise.resolve(unanswered());
  return new Promise((resolve) => {
    let chosen = false;
    const choose = (answer: Answer) => {
      if (chosen) return false;
      chosen = true;
      context.whenGone(null);
      resolve(answer);
      return true;
    };
    context.whenGone(() => choose(unanswered()));
    offer(choose);
  });
}
