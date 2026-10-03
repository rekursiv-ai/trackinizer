import { useId, useState } from "react";
import type { Peer } from "../api/detail";
import type { EdgeRef } from "../api/edges";
import { PriorityPicker, priorityText } from "../editors/Priority";
import { Menu } from "../lists/Menu";
import { LabelChip, LabelDot, PriorityGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { edgeAnnotationEdit, edgeLabelEdit, sameValue } from "../writes/edits";
import { useWrite } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";

/** An edge's annotations as the detail reads them from its far end; each is absent when unset. */
export type Annotations = Pick<Peer, "note" | "valence" | "labels" | "priority">;

/**
 * Edit one edge's annotations: its note, its valence, its labels and its
 * priority. Each control writes on its own, one request per change, with its
 * state beside it and an Undo on its toast: a note or valence is a `PUT` (an
 * empty note a `DELETE`), a label one `PATCH` per label, a priority a `PUT`, or
 * a `DELETE` for none.
 *
 * Valence shows on an edge that has one: the server stores one on every
 * citation, defaulting to 0.5, and none on any other edge. Priority shows when
 * `priority` says the edge kind carries one. `offered` are labels to suggest.
 *
 * While a control's write is under way, the control takes no input and its
 * buttons read as off: the writer takes no second edit, and a save that lands
 * marks what it saved as the stored value.
 */
export function EdgeAnnotations({
  edge,
  annotations,
  priority,
  offered,
  disabled,
}: {
  edge: EdgeRef;
  annotations: Annotations;
  priority: boolean;
  offered: readonly string[];
  disabled: boolean;
}) {
  return (
    <>
      <NoteField edge={edge} note={annotations.note} disabled={disabled} />
      {annotations.valence === undefined ? null : (
        <ValenceField edge={edge} valence={annotations.valence} disabled={disabled} />
      )}
      <LabelsField edge={edge} labels={annotations.labels ?? []} offered={offered} disabled={disabled} />
      {priority ? <PriorityField edge={edge} priority={annotations.priority} disabled={disabled} /> : null}
    </>
  );
}

/** A valence as the relation rows show it: `+0.5`, `-0.4`. */
export function signed(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}`;
}

function NoteField({ edge, note, disabled }: { edge: EdgeRef; note: string | undefined; disabled: boolean }) {
  const writer = useWrite();
  const pending = unsettled(writer.state);
  const [text, setText] = useFollowed(note ?? "");
  const id = useId();
  return (
    <form
      className="ea-field"
      onSubmit={async (event) => {
        event.preventDefault();
        const to = text.trim();
        if (pending || sameValue(to, note)) return;
        if (await writer.run(edgeAnnotationEdit({ edge, annotation: "note", label: "Note", from: note, to }))) setText(to, true);
      }}
    >
      <label htmlFor={id}>Note</label>
      <div className="ea-row">
        <input
          id={id}
          className="field"
          placeholder="Why this link exists"
          autoComplete="off"
          readOnly={pending}
          value={text}
          onChange={(event) => setText(event.target.value, false)}
        />
        <WriteButton type="submit" className="btn" pending={pending} disabled={disabled}>
          Save
        </WriteButton>
      </div>
      <WriteStatus state={writer.state} />
    </form>
  );
}

/** Valence in [-1, 1], in steps of 0.1: below 0 argues against. A citation always has one, so it is never cleared. */
function ValenceField({ edge, valence, disabled }: { edge: EdgeRef; valence: number; disabled: boolean }) {
  const writer = useWrite();
  const pending = unsettled(writer.state);
  const [value, setValue] = useFollowed(valence);
  const id = useId();
  return (
    <form
      className="ea-field"
      onSubmit={async (event) => {
        event.preventDefault();
        if (pending || value === valence) return;
        const edit = edgeAnnotationEdit({ edge, annotation: "valence", label: "Valence", from: valence, to: value, show: showValence });
        if (await writer.run(edit)) setValue(value, true);
      }}
    >
      <label htmlFor={id}>
        Valence <output className={`v-num ${value >= 0 ? "pos" : "neg"}`}>{signed(value)}</output>
      </label>
      <div className="ea-row">
        <input
          id={id}
          type="range"
          min={-1}
          max={1}
          step={0.1}
          // A slider has no read-only state; the Save pressed keeps focus.
          disabled={pending}
          value={value}
          onChange={(event) => setValue(Number(event.target.value), false)}
        />
        <WriteButton type="submit" className="btn" pending={pending} disabled={disabled}>
          Save
        </WriteButton>
      </div>
      <WriteStatus state={writer.state} />
    </form>
  );
}

/**
 * The edge's labels, each removable, and a menu to add one: picked from `offered`
 * or typed. One label per `PATCH`, never the whole list, so labels others add
 * meanwhile stay.
 */
function LabelsField({
  edge,
  labels,
  offered,
  disabled,
}: {
  edge: EdgeRef;
  labels: readonly string[];
  offered: readonly string[];
  disabled: boolean;
}) {
  const writer = useWrite();
  const pending = unsettled(writer.state);
  const change = (op: "add" | "sub", value: string) => void writer.run(edgeLabelEdit({ edge, op, value }));
  return (
    <div className="ea-field" role="group" aria-label="Labels">
      <span className="ea-k">Labels</span>
      <div className="ea-row ea-labels">
        {labels.map((label) => (
          <span key={label} className="ea-chip">
            <LabelChip label={label} />
            <WriteButton
              className="ea-x"
              aria-label={`Remove label ${label}`}
              pending={pending}
              disabled={disabled}
              onClick={() => change("sub", label)}
            >
              <Icon name="x" size={11} />
            </WriteButton>
          </span>
        ))}
        <Menu
          label="Add a label…"
          trigger={
            <WriteButton className="btn ghost" pending={pending} disabled={disabled}>
              <Icon name="plus" size={12} />
              Label
            </WriteButton>
          }
          options={offered
            .filter((label) => !labels.includes(label))
            .map((label) => ({ value: label, label, icon: <LabelDot label={label} /> }))}
          create={(typed) => `Create label “${typed}”`}
          onPick={(value) => change("add", value)}
        />
      </div>
      <WriteStatus state={writer.state} />
    </div>
  );
}

/** The child's priority under this parent: a band, an exact number, or none, which clears it. */
function PriorityField({ edge, priority, disabled }: { edge: EdgeRef; priority: number | undefined; disabled: boolean }) {
  const writer = useWrite();
  return (
    <div className="ea-field" role="group" aria-label="Priority">
      <span className="ea-k">Priority</span>
      <div className="ea-row">
        <PriorityPicker
          value={priority}
          trigger={
            <WriteButton className="btn" pending={unsettled(writer.state)} disabled={disabled} title="Priority under this parent">
              <PriorityGlyph priority={priority ?? null} />
              {priorityText(priority)}
            </WriteButton>
          }
          onPick={(to) => {
            if (!sameValue(to, priority)) {
              void writer.run(edgeAnnotationEdit({ edge, annotation: "priority", label: "Priority", from: priority, to, show: priorityText }));
            }
          }}
        />
      </div>
      <WriteStatus state={writer.state} />
    </div>
  );
}

/**
 * A control's draft of a stored value. It follows the stored value while it is
 * still the one last read or saved, so an Undo or someone else's change shows,
 * and a later Save cannot quietly re-apply what was undone. Once edited, it is
 * kept until saved. `set(draft, true)` marks a draft just saved as the new base.
 */
function useFollowed<Value>(stored: Value): [Value, (draft: Value, saved: boolean) => void] {
  const [state, setState] = useState({ base: stored, draft: stored });
  if (!Object.is(stored, state.base) && Object.is(state.draft, state.base)) setState({ base: stored, draft: stored });
  const set = (draft: Value, saved: boolean) => setState((held) => ({ base: saved ? draft : held.base, draft }));
  return [state.draft, set];
}

function showValence(value: unknown): string {
  return typeof value === "number" ? signed(value) : "the default";
}
