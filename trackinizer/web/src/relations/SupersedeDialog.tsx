import { Dialog } from "radix-ui";
import { useState } from "react";
import type { Detail } from "../api/detail";
import { useWriteMode } from "../app/boot";
import { keyCaps } from "../commands/registry";
import type { CreatableKind } from "../create/draft";
import { useRouter } from "../router/router";
import { kindLook } from "../ui/kinds";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { supersedeWithNewEdit } from "./edits";
import "./relations.css";

/**
 * Create a new inquiry of the same kind, `kind`, that supersedes `detail`'s, in
 * one batch request, then open it. The title starts as the old one's. The old
 * inquiry keeps its status: the edge alone marks it superseded, which takes it
 * out of `trax next` and out of the evidence that counts as currently true.
 *
 * A failure the automatic retries did not recover offers Retry and Discard.
 * Closed while the request is under way, the dialog opens nothing once it
 * lands; the toast still says it did.
 */
export function SupersedeDialog({
  detail,
  kind,
  returnTo,
  onClose,
}: {
  detail: Detail;
  kind: CreatableKind;
  returnTo: HTMLElement | null;
  onClose: () => void;
}) {
  const writer = useWrite();
  const { navigate } = useRouter();
  const writable = useWriteMode() === "enabled";
  const [title, setTitle] = useState(detail.self.title);
  const [description, setDescription] = useState("");
  const pending = unsettled(writer.state);
  const old = `${detail.self.kind}#${detail.self.seq}`;
  const one = kindLook(detail.self.kind).one;
  const create = async () => {
    if (pending || !writable || !title.trim()) return;
    const self = { id: detail.self.id, kind, seq: detail.self.seq };
    const result = await writer.run(supersedeWithNewEdit(self, { title: title.trim(), description: description.trim() }));
    if (!result) return;
    onClose();
    navigate({ name: "lookup", id: result.ids[0]! });
  };
  return (
    <Dialog.Root open onOpenChange={(open) => open || onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content
          className="modal"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (returnTo?.isConnected) returnTo.focus();
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                void create();
              }
            }}
          >
            <div className="modal-b">
              <Dialog.Title asChild>
                <h3>
                  Supersede {old} with a new {one}
                </h3>
              </Dialog.Title>
              <Dialog.Description asChild>
                <p>
                  The new {one} supersedes {old}, which keeps its status and shows as superseded. Both land together, or
                  neither does.
                </p>
              </Dialog.Description>
              <label className="sd-field">
                Title
                <input
                  className="field"
                  autoFocus
                  autoComplete="off"
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
              <label className="sd-field">
                Description
                <textarea
                  className="field"
                  placeholder="Markdown (optional)"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
              <WriteStatus state={writer.state} />
            </div>
            <div className="modal-f">
              <div className="spacer" />
              <Dialog.Close className="btn ghost">Cancel</Dialog.Close>
              <WriteButton type="submit" className="btn primary" pending={pending} disabled={!writable || !title.trim()}>
                Create and supersede <kbd>{keyCaps("$mod+Enter")[0]}</kbd>
              </WriteButton>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
