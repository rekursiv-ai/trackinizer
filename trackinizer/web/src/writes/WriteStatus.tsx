import { AlertDialog } from "radix-ui";
import { CopyDetails } from "../debug/CopyDetails";
import { useOnline } from "../ui/bars";
import type { WriteState } from "./useWrite";
import "./writes.css";

/**
 * Whether a control's write has not settled: it is being sent, or it failed and
 * shows Retry and Discard. Either way the control takes no input and makes no
 * second write: Retry sends what the control showed when the write failed, so
 * the control must still show exactly that, and Discard hands it back. Every
 * write control reads its `pending` from here.
 */
export function unsettled(state: WriteState): boolean {
  return state.status === "pending" || state.status === "failed";
}

/**
 * One control's write, shown next to the control: Saving…, the server's message
 * with Copy details, Retry and Discard, or the conflict dialog.
 *
 * Retry is a write, so it is off while the browser is offline.
 */
export function WriteStatus({ state }: { state: WriteState }) {
  const online = useOnline();
  switch (state.status) {
    case "idle":
      return null;
    case "pending":
      return (
        <span className="w-status" role="status">
          Saving…
        </span>
      );
    case "rejected":
      // The alert is the message alone: it offers no Retry, and asking again
      // gets the same answer.
      return (
        <div className="w-status">
          <span role="alert">
            <Lines text={state.message} />
          </span>
          <CopyDetails message={state.message} error={state.error} />
        </div>
      );
    case "failed":
      return (
        <div className="w-status" role="alert">
          <Lines text={state.message} />
          <span className="w-actions">
            <button type="button" className="btn" onClick={state.retry} disabled={!online}>
              Retry
            </button>
            <button type="button" className="btn ghost" onClick={state.discard}>
              Discard
            </button>
            <CopyDetails message={state.message} error={state.error} />
          </span>
        </div>
      );
    case "conflict":
      return <ConflictDialog state={state} online={online} />;
  }
}

/** The server's message, one line per field for a 422. */
function Lines({ text }: { text: string }) {
  return (
    <span className="form-err">
      {text.split("\n").map((line, index) => (
        <span key={index}>{line}</span>
      ))}
    </span>
  );
}

/**
 * The stored value is not the one editing began from: say who changed it, when
 * known, show both values, and ask. Keeping theirs is the default, and Escape
 * chooses it.
 */
function ConflictDialog({ state, online }: { state: Extract<WriteState, { status: "conflict" }>; online: boolean }) {
  const { label, who, theirs, mine } = state;
  return (
    <AlertDialog.Root open onOpenChange={(open) => open || state.keepTheirs()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="modal-backdrop" />
        <AlertDialog.Content className="modal small">
          <div className="modal-b">
            <AlertDialog.Title asChild>
              <h3>{label} changed</h3>
            </AlertDialog.Title>
            <AlertDialog.Description>
              {who ? `${who} changed this since you saw it.` : "It changed since you started editing."} Save yours
              anyway?
            </AlertDialog.Description>
            <dl className="w-versions">
              <dt>{who ? `Now, by ${who}` : "Now"}</dt>
              <dd>{theirs}</dd>
              <dt>Yours</dt>
              <dd>{mine}</dd>
            </dl>
          </div>
          <div className="modal-f">
            <div className="spacer" />
            <AlertDialog.Cancel className="btn ghost" onClick={state.keepTheirs}>
              Keep theirs
            </AlertDialog.Cancel>
            <button type="button" className="btn primary" onClick={state.saveMine} disabled={!online}>
              Save mine
            </button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
