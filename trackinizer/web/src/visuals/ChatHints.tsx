import { useEffect, useRef, useState } from "react";
import { partnerLabel, providerName, stationProvider } from "../api/providers";
import type { WorkspaceState } from "../api/workspaces";
import { ProviderPicker } from "./ProviderPicker";

/**
 * The row under the box: what this chat talks to, then the keys the box answers.
 *
 * The design puts the model here rather than in the column's top, so the strip above says only
 * which chat you are in and this says who it talks to. Pressing the name opens the provider card
 * upward, over the log, since the row stands at the bottom of the column.
 */
export function ChatHints({ workspace }: { readonly workspace: WorkspaceState | null }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && root.current?.contains(event.target)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [open]);
  if (!workspace) return null;
  const label = partnerLabel(workspace.partner, providerName(stationProvider(workspace), workspace));
  return <div className="chat-hints">
    <span className="chat-hints-pick" ref={root}>
      <button type="button" className="prov" aria-label={`Chat provider: ${label}`} aria-expanded={open}
        title="Choose what this chat talks to" onClick={() => setOpen((current) => !current)}>
        <span className="prov-label">{label}</span><span className="prov-caret" aria-hidden="true">▾</span>
      </button>
      {open && <div className="station-menu chat-hints-menu" role="dialog" aria-label="Chat provider card">
        <ProviderPicker station={workspace} />
      </div>}
    </span>
    <span>↵ send</span>
    <span>⇧↵ line</span>
  </div>;
}
