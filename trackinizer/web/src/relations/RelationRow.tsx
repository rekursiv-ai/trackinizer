import { type ReactNode, useRef, useState } from "react";
import type { Detail, Peer } from "../api/detail";
import type { EdgeRef } from "../api/edges";
import { useMeta } from "../app/boot";
import type { RelationGroup } from "../detail/relationGroups";
import { Icon } from "../ui/icons";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { EdgeAnnotations } from "./Annotations";
import { removeRelationEdit } from "./edits";
import { carriesPriority, provenanceLeft, relationEdge } from "./topology";
import "./relations.css";

/**
 * One relation in the detail's list, as list items: the row, with `children`
 * (the link to the inquiry at the other end), then Annotate and Remove, and the
 * open panel's own item under it. Annotate opens the edge's annotations; Remove
 * asks first, and says when a provenance edge between the two stays behind.
 * `mode` is the detail's write mode: a viewer gets the link alone, and offline
 * the actions are off.
 */
export function RelationRow({
  detail,
  group,
  peer,
  mode,
  children,
}: {
  detail: Detail;
  group: RelationGroup;
  peer: Peer;
  mode: "hidden" | "disabled" | "enabled";
  children: ReactNode;
}) {
  // A row's actions mount the first time it is hovered, focused or tapped, not
  // with it: on a 128-row hub the two icon buttons per row took the render from
  // 81 to 120 ms (jsdom, measured), and the largest hub has 731 rows. Focusing
  // the link mounts them, so Tab still reaches them next. A touch has no hover:
  // its pointerover comes with the tap, whose click then lands on the button
  // that mounts under the finger. So a touch mounts them by a tap on their empty
  // place, where the stylesheet draws a ⋯, and that click presses nothing.
  const [armed, setArmed] = useState(false);
  const [open, setOpen] = useState<"annotate" | "remove" | null>(null);
  const arm = mode === "hidden" || armed ? undefined : () => setArmed(true);
  const edge = relationEdge(detail.self.id, group, peer.id);
  const ref = refOf(peer);
  const what = `${group.label.toLowerCase()} ${ref}`;
  const toggle = (panel: "annotate" | "remove") => setOpen(open === panel ? null : panel);
  const buttons = { annotate: useRef<HTMLButtonElement>(null), remove: useRef<HTMLButtonElement>(null) };
  // A panel closed from inside gives focus back to the button that opened it.
  const close = (panel: "annotate" | "remove") => () => {
    setOpen(null);
    buttons[panel].current?.focus();
  };
  return (
    <>
      <li className="rel-row" onPointerOver={arm && ((event) => event.pointerType === "touch" || arm())} onFocus={arm}>
        {children}
        {mode === "hidden" ? null : (
          <span className="rel-actions" onClick={arm}>
            {armed ? (
              <>
                <button
                  ref={buttons.annotate}
                  type="button"
                  className="icon-btn"
                  title="Annotate"
                  aria-label={`Annotate ${what}`}
                  aria-expanded={open === "annotate"}
                  disabled={mode === "disabled"}
                  onClick={() => toggle("annotate")}
                >
                  <Icon name="edit" size={13} />
                </button>
                <button
                  ref={buttons.remove}
                  type="button"
                  className="icon-btn"
                  title="Remove"
                  aria-label={`Remove ${what}`}
                  aria-expanded={open === "remove"}
                  disabled={mode === "disabled"}
                  onClick={() => toggle("remove")}
                >
                  <Icon name="x" size={14} />
                </button>
              </>
            ) : null}
          </span>
        )}
      </li>
      {open === "remove" ? (
        <li className="rel-panel">
          <RemoveConfirm
            detail={detail}
            edge={edge}
            peer={peer}
            label={group.label}
            disabled={mode === "disabled"}
            onCancel={close("remove")}
          />
        </li>
      ) : null}
      {open === "annotate" ? (
        <li className="rel-panel">
          <AnnotatePanel
            detail={detail}
            edge={edge}
            peer={peer}
            label={group.label}
            disabled={mode === "disabled"}
            onDone={close("annotate")}
          />
        </li>
      ) : null}
    </>
  );
}

/**
 * Ask before removing `edge`, with an optional reason. A provenance edge between
 * the same two inquiries is a relation of its own: say that it stays, since
 * trackinizer may have inferred it when their first relation was drawn.
 */
function RemoveConfirm({
  detail,
  edge,
  peer,
  label,
  disabled,
  onCancel,
}: {
  detail: Detail;
  edge: EdgeRef;
  peer: Peer;
  label: string;
  disabled: boolean;
  onCancel: () => void;
}) {
  const writer = useWrite();
  const [reason, setReason] = useState("");
  const pending = unsettled(writer.state);
  const kept = provenanceLeft(detail, edge, peer);
  const self = refOf(detail.self);
  const ref = refOf(peer);
  const what = `${self} ${label.toLowerCase()} ${ref}`;
  return (
    <form
      className="confirm-row"
      aria-label={`Remove ${what}`}
      onSubmit={(event) => {
        event.preventDefault();
        if (!pending) void writer.run(removeRelationEdit(edge, { done: `Removed: ${what}`, reason: reason.trim() }));
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape" || event.nativeEvent.isComposing) return;
        // Escape on a button here would also reach the detail's Back shortcut.
        event.stopPropagation();
        onCancel();
      }}
    >
      <p className="rc-q">
        Remove the relation <b>{what}</b>?
      </p>
      {kept ? (
        <p className="rc-kept" role="note">
          {kept.from === detail.self.id ? `${self} stays produced by ${ref}` : `${ref} stays produced by ${self}`}. That
          provenance relation is separate, and trackinizer may have added it when the first relation between the two was
          drawn. Remove it on its own if it is wrong.
        </p>
      ) : null}
      <div className="rc-row">
        <input
          className="field"
          aria-label="Reason"
          placeholder="Reason (optional)"
          autoComplete="off"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
        <button type="button" className="btn ghost" onClick={onCancel} autoFocus>
          Cancel
        </button>
        <WriteButton type="submit" className="btn danger" pending={pending} disabled={disabled}>
          Remove
        </WriteButton>
      </div>
      <WriteStatus state={writer.state} />
    </form>
  );
}

/** The edge's annotations under its row, with Done to close them. Labels on the detail's other edges are offered. */
function AnnotatePanel({
  detail,
  edge,
  peer,
  label,
  disabled,
  onDone,
}: {
  detail: Detail;
  edge: EdgeRef;
  peer: Peer;
  label: string;
  disabled: boolean;
  onDone: () => void;
}) {
  const { edges, fieldOwners } = useMeta();
  const offered = [
    ...new Set(
      [...Object.values(detail.edges), ...Object.values(detail.backlinks)].flatMap((peers) => peers.flatMap((p) => p.labels ?? [])),
    ),
  ].sort((a, b) => a.localeCompare(b));
  return (
    <div className="edge-edit" role="group" aria-label={`Annotations of ${label.toLowerCase()} ${refOf(peer)}`}>
      <EdgeAnnotations
        edge={edge}
        annotations={peer}
        priority={carriesPriority(edge.kind, edges, fieldOwners)}
        offered={offered}
        disabled={disabled}
      />
      <div className="actions">
        <button type="button" className="btn" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

function refOf({ kind, seq }: { kind: string; seq: number }): string {
  return `${kind}#${seq}`;
}
