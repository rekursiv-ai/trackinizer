import { useQueryClient } from "@tanstack/react-query";
import { Dialog } from "radix-ui";
import { useState } from "react";
import type { Detail } from "../api/detail";
import { useWriteMode } from "../app/boot";
import { keyCaps } from "../commands/registry";
import { isUnset } from "../detail/fields";
import { detailQueries } from "../detail/queries";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { clearOwnerEdit, purgeEdit } from "./edits";
import "./bulk.css";

/**
 * Purge `detail`'s inquiry, as the mock's ⋯ menu does: ask once, with a reason
 * the purge requires, then delete the row and every edge touching it.
 *
 * The server refuses to purge an owned row (409), so for one the dialog says
 * who owns it and offers to clear the owner first: a compare-and-set write
 * with the same reason, then the purge. Once it lands the detail refetches and
 * says the inquiry is gone, and lists showing it refetch without it. Closed
 * while the owner is cleared, the dialog purges nothing.
 */
export function PurgeDialog({
  detail,
  returnTo,
  onClose,
}: {
  detail: Detail;
  returnTo: HTMLElement | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const writer = useWrite();
  const writable = useWriteMode() === "enabled";
  const [reason, setReason] = useState("");
  const [missing, setMissing] = useState(false);
  const self = detail.self;
  const name = `${self.kind}#${self.seq}`;
  const owner = isUnset(self.owner) ? null : String(self.owner);
  const pending = unsettled(writer.state);
  const purge = async () => {
    if (pending || !writable) return;
    const why = reason.trim();
    if (!why) {
      setMissing(true);
      return;
    }
    // Each resolves null too once the dialog is closed, so a closed one purges nothing.
    if (owner !== null && (await writer.run(clearOwnerEdit(self, why))) === null) return;
    if ((await writer.run(purgeEdit(self, why))) === null) {
      // Refused, perhaps because someone has since taken it: show who owns it now.
      void queryClient.invalidateQueries({ queryKey: detailQueries.detail(self.id).queryKey });
      return;
    }
    onClose();
  };
  return (
    <Dialog.Root open onOpenChange={(open) => open || onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content
          className="modal small"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (returnTo?.isConnected) returnTo.focus();
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void purge();
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                void purge();
              }
            }}
          >
            <div className="modal-b">
              <Dialog.Title asChild>
                <h3>Purge {name}</h3>
              </Dialog.Title>
              <Dialog.Description asChild>
                <p>Purge deletes the row and every edge touching it. To keep history, set the status to Invalid instead.</p>
              </Dialog.Description>
              {owner !== null ? (
                <p className="pg-owner">
                  {name} is owned by <b>{owner}</b>. Trackinizer purges only rows without an owner, so the owner is
                  cleared first.
                </p>
              ) : null}
              <textarea
                aria-label="Reason"
                className="field pg-reason"
                placeholder="Reason (required, shown in activity)"
                autoFocus
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                  setMissing(false);
                }}
              />
              {missing ? (
                <span className="form-err" role="alert">
                  Add a reason first.
                </span>
              ) : null}
              <WriteStatus state={writer.state} />
            </div>
            <div className="modal-f">
              <div className="spacer" />
              <Dialog.Close className="btn ghost">Cancel</Dialog.Close>
              <WriteButton type="submit" className="btn danger" pending={pending} disabled={!writable}>
                {owner !== null ? "Clear owner and purge" : "Purge permanently"} <kbd>{keyCaps("$mod+Enter")[0]}</kbd>
              </WriteButton>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
