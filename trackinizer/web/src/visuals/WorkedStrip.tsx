import { useQuery } from "@tanstack/react-query";
import type { components } from "../api/generated/schema";
import type { WorkspaceState } from "../api/workspaces";
import { detailQueries } from "../detail/queries";

/** A record a canvas worked on, and the visual that showed it (`server/visuals/workspaces.py`). */
export type WorkedRecord = components["schemas"]["WorkedRecord"];

/** A chip of the strip: the record, and the pane showing it now, if one does. */
type Chip = { readonly record: WorkedRecord; readonly pane: string | null };

/**
 * The strip's chips: first the records on the canvas's docked panes, in the
 * order the panes stand, then the rest the canvas worked on, the latest first.
 */
export function stripOf(worked: readonly WorkedRecord[], visuals: WorkspaceState["visuals"]): readonly Chip[] {
  const shown: Chip[] = visuals
    .filter((visual) => visual.record_id && (visual.placement ?? "main") !== "floating")
    .map((visual) => ({ record: { record_id: visual.record_id!, visual_type: visual.type }, pane: visual.id }));
  const rest = worked.filter((record) => !shown.some((chip) => same(chip.record, record))).map((record) => ({ record, pane: null }));
  return [...shown, ...rest];
}

/**
 * The records a canvas worked on, closed ones included, as a chip each: the
 * ones on the canvas first, pressed, in the order their panes stand. A click on
 * one shown focuses its pane; on one closed, shows it in a pane of its own.
 */
export function WorkedStrip({ worked, visuals, viewOf, disabled, onShow, onFocus }: {
  readonly worked: readonly WorkedRecord[];
  readonly visuals: WorkspaceState["visuals"];
  /** A short name for the view a visual type draws, which a chip shows before its record. */
  readonly viewOf: (visualType: string) => string;
  readonly disabled: boolean;
  readonly onShow: (record: WorkedRecord) => void;
  readonly onFocus: (pane: string) => void;
}) {
  const chips = stripOf(worked, visuals);
  if (!chips.length) return null;
  return (
    <nav className="canvas-worked" aria-label="Worked on">
      {chips.map(({ record, pane }) => <WorkedChip key={`${record.visual_type}:${record.record_id}`} record={record} view={viewOf(record.visual_type)} shown={pane !== null}
        disabled={disabled} onClick={() => (pane ? onFocus(pane) : onShow(record))} />)}
    </nav>
  );
}

/** A record's `Kind#seq title` once it is read, or null until then. */
export function useRecordName(recordId: string): string | null {
  const self = useQuery({ ...detailQueries.detail(recordId), retry: false }).data?.self;
  return self ? `${self.kind}#${self.seq} ${self.title}` : null;
}

/** One record's chip: the view that showed it, then the record, so two views of one record read apart. */
function WorkedChip({ record, view, shown, disabled, onClick }: {
  readonly record: WorkedRecord;
  readonly view: string;
  readonly shown: boolean;
  readonly disabled: boolean;
  readonly onClick: () => void;
}) {
  const name = useRecordName(record.record_id) ?? "…";
  return (
    <button type="button" className="canvas-worked-chip" aria-pressed={shown} disabled={disabled}
      title={`${view}: ${name}${shown ? " (on the canvas; click to focus)" : " (click to show)"}`} onClick={onClick}>
      <span className="canvas-worked-view">{view}</span> {name}
    </button>
  );
}

/** The record a pane shows, beside its view's name in the pane's bar. */
export function PaneRecord({ recordId }: { readonly recordId: string }) {
  const name = useRecordName(recordId);
  return name ? <span className="visual-tile-record">{name}</span> : null;
}

function same(left: WorkedRecord, right: WorkedRecord): boolean {
  return left.record_id === right.record_id && left.visual_type === right.visual_type;
}
