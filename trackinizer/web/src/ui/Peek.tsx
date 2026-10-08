import { type ReactNode, Suspense, useId, useState } from "react";
import { CommandRegistry, CommandRegistryContext } from "../commands/registry";
import { formatRoute } from "../router/route";
import { DetailView } from "../router/views";
import { Icon } from "./icons";
import { type Panel, type PanelSpec, PanelStrip, PanelToggle } from "./panel";
import "./peek.css";

/** Peek as a panel: on the right of a list or the graph, and `]` collapses or expands it. */
export const PEEK: PanelSpec = { id: "peek", name: "Peek", side: "right", keys: ["]"] };

/**
 * An inquiry's detail in a panel beside a list or the graph, with its ref and
 * buttons that collapse and close it; the caller's Escape closes it. Collapsed
 * (`panel`), a strip that keeps the ref and the close button takes its place, and
 * the detail stays mounted, hidden, so an edit begun in it is there on expanding.
 *
 * `body` replaces the detail with the caller's own read of the record — the
 * graph's panel, which is one view of the record, not the detail with insights
 * stacked on it. Given a body, the caller owns the commands too: `actions` and
 * the Open link move out of the bar and into `foot`, its own row at the panel's
 * foot. `crumbs` joins the bar either way: the graph puts the thread's name there.
 */
export function Peek({
  row,
  panel,
  onClose,
  crumbs = null,
  actions = null,
  body = null,
  foot = null,
}: {
  row: { readonly kind: string; readonly seq: number };
  panel: Panel;
  onClose: () => void;
  crumbs?: ReactNode;
  actions?: ReactNode;
  body?: ReactNode;
  foot?: ReactNode;
}) {
  const ref = { name: "ref", kind: row.kind, seq: row.seq } as const;
  const id = useId();
  // The detail's keys are a page's (its Escape goes back to the list), and being
  // mounted last they would win over the caller's own Escape, which closes the peek.
  // A registry of its own, bound to no keys, keeps the peek from acting as a page.
  const [detailCommands] = useState(() => new CommandRegistry());
  const close = (
    <button type="button" className="icon-btn" onClick={onClose} aria-label="Close peek">
      <Icon name="x" />
    </button>
  );
  const collapsed = panel.collapsed;
  return (
    <>
      {collapsed ? (
        <PanelStrip panel={panel} id={id} label="Peek" className="peek-strip">
          {close}
          <span className="panel-strip-text mono">
            {row.kind}#{row.seq}
          </span>
        </PanelStrip>
      ) : null}
      {/* Collapsed, the strip takes its id; it drops `.peek`, whose display: flex
          would show it and whose rules (graph.css, live.css) place an open Peek;
          and its bar goes, as two toggles would share the one `Panel.button`. */}
      <aside id={collapsed ? undefined : id} className={collapsed ? undefined : "peek"} aria-label="Peek" hidden={collapsed}>
        {collapsed ? null : (
          <div className="peek-bar">
            {crumbs}
            <span className="mono">
              {row.kind}#{row.seq}
            </span>
            <span className="spacer" />
            {body === null ? (
              <>
                {actions}
                <a className="btn ghost" href={formatRoute(ref)}>
                  Open
                </a>
              </>
            ) : null}
            <PanelToggle panel={panel} controls={id} />
            {close}
          </div>
        )}
        <CommandRegistryContext value={detailCommands}>
          {body === null ? (
            // The detail's code is a chunk of its own (src/router/views.ts); the peek's bar shows while it loads.
            <Suspense>
              <DetailView target={{ kind: row.kind, seq: row.seq }} />
            </Suspense>
          ) : (
            body
          )}
          {foot === null ? null : <div className="peek-foot">{foot}</div>}
        </CommandRegistryContext>
      </aside>
    </>
  );
}
