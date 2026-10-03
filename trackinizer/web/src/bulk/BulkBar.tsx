import { useQueryClient } from "@tanstack/react-query";
import { type ReactElement, type ReactNode, type Ref, useRef, useState } from "react";
import type { InquiryRow } from "../api/inquiries";
import { useMeta, useWriteMode } from "../app/boot";
import { CopyDetails } from "../debug/CopyDetails";
import { PriorityPicker, priorityText } from "../editors/Priority";
import { type ReasonAsk, ReasonDialog } from "../editors/Reason";
import { loadedValues } from "../editors/values";
import { Menu } from "../lists/Menu";
import { PeoplePicker } from "../people/Picker";
import { useOnline } from "../ui/bars";
import { capitalize, LabelDot, PriorityGlyph, StatusGlyph } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { WriteButton } from "../writes/WriteButton";
import { type BulkChange, setEach, toggleLabel } from "./edits";
import { type BulkReport, landed, type Outcome, retryCount, useBulkEdit } from "./run";
import "./bulk.css";

/**
 * The mock's bulk bar under a list with rows selected: change status, priority
 * (when every row is an Issue), labels or owner on all of them at once, one
 * request per row. While they are sent it says how far it got; once they are
 * back, a toast says so, or, when any failed, a report lists each row's result
 * with Retry for the ones that failed. The actions stay off until the report is
 * retried or closed, so a new change never replaces it.
 */
export function BulkBar({ rows, onClear }: { rows: readonly InquiryRow[]; onClear: () => void }) {
  const bulk = useBulkEdit();
  const offline = useWriteMode() === "disabled";
  const { report } = bulk;
  const running = report?.running ?? false;
  if (rows.length === 0 && !report) return null;
  const apply = (change: BulkChange, at: readonly InquiryRow[] = rows) => bulk.start(change, at);
  const button = (icon: ReactNode, label: string, ref?: Ref<HTMLButtonElement>): ReactElement => (
    <WriteButton ref={ref} className="btn ghost" pending={report !== null} disabled={offline}>
      {/* The glyphs name a value (Active, P1 High); here they only mark the action. */}
      <span className="bulk-ic" aria-hidden="true">
        {icon}
      </span>
      {label}
    </WriteButton>
  );
  return (
    <div className="bulk-dock">
      {report && !running ? <Report report={report} onRetry={bulk.retry} onClose={bulk.dismiss} /> : null}
      {rows.length > 0 || running ? (
        <div className="bulk" role="group" aria-label="Bulk actions">
          {rows.length > 0 ? (
            <>
              <span className="sel-count">
                {rows.length} selected
                <button type="button" className="icon-btn" aria-label="Clear selection" onClick={onClear}>
                  <Icon name="x" size={13} />
                </button>
              </span>
              <StatusAction rows={rows} button={button} apply={apply} />
              {rows.every((row) => "priority" in row) ? (
                <PriorityPicker
                  value={shared(rows.map((row) => row.priority ?? undefined))}
                  mixed={new Set(rows.map((row) => row.priority ?? null)).size > 1}
                  trigger={button(<PriorityGlyph priority={10} />, "Priority")}
                  onPick={(to) => apply(setEach("priority", to, { label: "Priority", show: priorityText }))}
                />
              ) : null}
              <LabelsAction rows={rows} button={button} apply={apply} />
              <OwnerAction rows={rows} button={button} apply={apply} />
            </>
          ) : null}
          {running ? <Progress report={report!} /> : null}
        </div>
      ) : null}
    </div>
  );
}

type ActionProps = {
  rows: readonly InquiryRow[];
  button: (icon: ReactNode, label: string, ref?: Ref<HTMLButtonElement>) => ReactElement;
  /** Apply `change` to `at`, the rows as they were when it was chosen; by default, as shown now. */
  apply: (change: BulkChange, at?: readonly InquiryRow[]) => void;
};

/**
 * Status for every row; abandoning and invalidating ask for a reason first, as
 * the detail does. The rows are guarded as they were when the status was
 * picked, not as a refetch shows them once the reason is given.
 */
function StatusAction({ rows, button, apply }: ActionProps) {
  const { enums } = useMeta();
  const returnFocus = useRef<HTMLButtonElement>(null);
  const [asking, setAsking] = useState<{ readonly to: string; readonly rows: readonly InquiryRow[]; readonly ask: ReasonAsk } | null>(null);
  const what = rows.length === 1 ? `${rows[0]!.kind}#${rows[0]!.seq}` : `${rows.length} inquiries`;
  const set = (to: string, reason?: string, at = rows) =>
    apply(setEach("status", to, { label: "Status", show: (value) => capitalize(String(value)), reason }), at);
  return (
    <>
      <Menu
        label="Change status…"
        trigger={button(<StatusGlyph status="active" />, "Status", returnFocus)}
        options={(enums.status ?? []).map((value) => ({
          value,
          label: capitalize(value),
          icon: <StatusGlyph status={value} />,
          checked: rows.every((row) => row.status === value),
        }))}
        onPick={(to) =>
          to === "abandoned" || to === "invalid"
            ? setAsking({
                to,
                rows,
                ask: {
                  title: `${to === "invalid" ? "Invalidate" : "Abandon"} ${what}`,
                  body:
                    to === "invalid"
                      ? "Invalid means the inquiry was wrong from the start. The reason is kept in activity."
                      : "Abandoned keeps the history but takes it off active lists.",
                  confirm: to === "invalid" ? "Invalidate" : "Abandon",
                  danger: to === "invalid",
                },
              })
            : set(to)
        }
      />
      <ReasonDialog
        ask={asking?.ask ?? null}
        returnFocus={returnFocus}
        onCancel={() => setAsking(null)}
        onConfirm={(reason) => {
          setAsking(null);
          set(asking!.to, reason, asking!.rows);
        }}
      />
    </>
  );
}

/**
 * A label for every row, from the labels already loaded or typed. Picking one
 * every row has removes it from each; otherwise it goes on those that lack it.
 */
function LabelsAction({ rows, button, apply }: ActionProps) {
  const queryClient = useQueryClient();
  const labels = new Set([...rows.flatMap((row) => row.labels ?? []), ...loadedValues(queryClient, "labels")]);
  return (
    <Menu
      label="Add or remove a label…"
      trigger={button(<Icon name="tag" size={14} />, "Labels")}
      options={[...labels].sort((a, b) => a.localeCompare(b)).map((label) => ({
        value: label,
        label,
        icon: <LabelDot label={label} />,
        checked: rows.every((row) => (row.labels ?? []).includes(label)),
      }))}
      create={(typed) => `Create label “${typed}”`}
      onPick={(label) => apply(toggleLabel(label, rows))}
    />
  );
}

/**
 * An owner for every row, from the people picker, or none. "Me" writes the
 * signed-in email. Each row is guarded against its owner as shown when the
 * pick was made, one added through the New person dialog too.
 */
function OwnerAction({ rows, button, apply }: ActionProps) {
  const owners = rows.map((row) => row.owner ?? null);
  return (
    <PeoplePicker
      field="owner"
      trigger={button(<Icon name="user" size={14} />, "Owner")}
      current={owners.filter((owner) => owner !== null)}
      checked={(actor) => owners.every((owner) => owner === actor)}
      none="No owner"
      onPick={(to) => apply(setEach("owner", to, { label: "Owner", show: String }))}
    />
  );
}

/** How far a bulk edit has got. */
function Progress({ report }: { report: BulkReport }) {
  const settled = report.entries.filter((entry) => entry.outcome.status !== "pending").length;
  return (
    <span className="w-status bulk-progress" role="status">
      Saving… {settled} of {report.entries.length}
    </span>
  );
}

/**
 * Each row's result, the rows that did not land first, with Retry for those a
 * retry can change: the ones that failed, and the ones someone changed first.
 * Retry is a write, so it is off while offline.
 */
function Report({ report, onRetry, onClose }: { report: BulkReport; onRetry: () => void; onClose: () => void }) {
  const online = useOnline();
  const { title, entries } = report;
  const failed = entries.filter((entry) => !landed(entry));
  const retries = retryCount(entries);
  return (
    <section className="bulk-report" aria-label={`${title}: results`}>
      <p className="br-head" role="alert">
        <b>{title}</b>: {failed.length} of {entries.length} not saved.
      </p>
      <ul className="br-rows">
        {[...failed, ...entries.filter(landed)].map(({ row, outcome }) => (
          <li key={row.id} className={`br-row is-${outcome.status}`}>
            <Icon name={outcome.status === "done" || outcome.status === "unchanged" ? "check" : "x"} size={13} />
            <span className="mono">
              {row.kind}#{row.seq}
            </span>
            <span className="br-title">{row.title}</span>
            <span className="br-msg">{resultText(outcome)}</span>
          </li>
        ))}
      </ul>
      <div className="br-foot">
        <CopyDetails message={failedText(title, entries.length, failed)} error={null} />
        <span className="spacer" />
        <button type="button" className="btn ghost" onClick={onClose}>
          Close
        </button>
        {retries > 0 ? (
          <button type="button" className="btn primary" onClick={onRetry} disabled={!online}>
            Retry {retries} failed
          </button>
        ) : null}
      </div>
    </section>
  );
}

/**
 * The report for Copy details: each row not saved, by ref and id, never title,
 * with why. The failed requests, with their ids, are in the recent events.
 */
function failedText(title: string, total: number, failed: readonly { row: InquiryRow; outcome: Outcome }[]): string {
  const rows = failed.map(({ row, outcome }) => `${row.kind}#${row.seq} ${row.id}: ${resultText(outcome)}`);
  return [`${title}: ${failed.length} of ${total} not saved.`, ...rows].join("\n");
}

function resultText(outcome: Outcome): string {
  switch (outcome.status) {
    case "pending":
      return "Saving…";
    case "done":
      return "Saved";
    case "unchanged":
      return "Already so";
  }
  return outcome.message;
}

/** The one value every row has, or `undefined` when they differ. */
function shared<Value>(values: readonly Value[]): Value | undefined {
  return values.every((value) => value === values[0]) ? values[0] : undefined;
}
