import { Dialog } from "radix-ui";
import { type RefObject, useState } from "react";
import { keyCaps } from "../commands/registry";

/** What the reason dialog asks: its title, why it asks, and its confirm button. */
export type ReasonAsk = {
  readonly title: string;
  readonly body: string;
  readonly confirm: string;
  /** Draws the confirm button as destructive. */
  readonly danger?: boolean;
};

/**
 * Ask why before a change the plan wants explained: abandoning, invalidating,
 * or a judgement. The reason is optional, as in the old UI; the server keeps it
 * on the change, and activity shows it. ⌘↵ confirms, Escape cancels.
 *
 * `returnFocus` gets focus back on close: the dialog opens from a menu pick, so
 * no Radix trigger of its own would take it.
 */
export function ReasonDialog({
  ask,
  onConfirm,
  onCancel,
  returnFocus,
}: {
  ask: ReasonAsk | null;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
  returnFocus: RefObject<HTMLElement | null>;
}) {
  const [reason, setReason] = useState("");
  if (!ask) return null;
  const confirm = () => {
    onConfirm(reason.trim());
    setReason("");
  };
  return (
    <Dialog.Root
      open
      onOpenChange={(open) => {
        if (open) return;
        setReason("");
        onCancel();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content
          className="modal small"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            returnFocus.current?.focus();
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              confirm();
            }}
          >
            <div className="modal-b">
              <Dialog.Title asChild>
                <h3>{ask.title}</h3>
              </Dialog.Title>
              <Dialog.Description asChild>
                <p>{ask.body}</p>
              </Dialog.Description>
              <textarea
                aria-label="Reason"
                className="field ed-reason"
                placeholder="Reason (optional, shown in activity)"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                onKeyDown={(event) => {
                  if (event.nativeEvent.isComposing) return;
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault();
                    confirm();
                  }
                }}
              />
            </div>
            <div className="modal-f">
              <div className="spacer" />
              <Dialog.Close className="btn ghost">Cancel</Dialog.Close>
              <button type="submit" className={ask.danger ? "btn danger" : "btn primary"}>
                {ask.confirm} <kbd>{keyCaps("$mod+Enter")[0]}</kbd>
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
