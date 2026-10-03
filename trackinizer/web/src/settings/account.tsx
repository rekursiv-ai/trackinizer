import { type QueryKey, type UseQueryResult, useQueryClient } from "@tanstack/react-query";
import { AlertDialog } from "radix-ui";
import { type ReactNode, useState } from "react";
import type { Role } from "../api/me";
import { CopyDetails } from "../debug/CopyDetails";
import type { Edit } from "../writes/edits";
import { useWrite, type WriteState } from "../writes/useWrite";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { rolesUpTo } from "./roles";

/**
 * One account write (a token, a user, the allowlist) as the write layer sends
 * it: through `useWrite`, never retried on its own, since the server keeps no
 * key for these routes. `done` is the toast once it lands.
 */
export function accountEdit<Result>(send: () => Promise<Result>, done?: string): Edit<Result> {
  return { request: { route: "account", send }, touches: [], ...(done !== undefined && { done }) };
}

/** One control's account writes: its state, and `run`, which resolves true once one landed. */
export type AccountWriter = {
  readonly state: WriteState;
  readonly run: <Result>(edit: Edit<Result>) => Promise<boolean>;
};

/**
 * Write through the write layer, then refetch `list`, the query the control's
 * row is in. Account data is not in the live stream, so a screen shows a change
 * by refetching after the user's own writes: after each one, whatever its
 * outcome, since one whose answer was lost may have landed.
 */
export function useAccountWriter(list: QueryKey): AccountWriter {
  const writer = useWrite();
  const queryClient = useQueryClient();
  return {
    state: writer.state,
    run: async (edit) => {
      // A write that resolves with nothing (a revoke) landed too; only null did not.
      const landed = (await writer.run(edit)) !== null;
      await queryClient.invalidateQueries({ queryKey: list });
      return landed;
    },
  };
}

/**
 * A failed read's message, with Retry and Copy details; nothing while it has not
 * failed. It goes above what the read shows, not in its place: rows already read
 * stay on screen through a failed refetch.
 */
export function ReadError({ read }: { read: Pick<UseQueryResult, "error" | "refetch"> }) {
  if (!read.error) return null;
  return (
    <p className="st-note form-err" role="alert">
      {read.error.message}{" "}
      <button type="button" className="btn" onClick={() => void read.refetch()}>
        Retry
      </button>
      <CopyDetails message={read.error.message} error={read.error} />
    </p>
  );
}

/** A settings section: a heading, its actions, and its body. */
export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="st-sec" aria-label={title}>
      <div className="st-h">
        <div>
          <h3>{title}</h3>
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** One line of a section: a label and its value. */
export function KeyValue({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="st-kv">
      <span>{label}</span>
      <span>{children}</span>
    </div>
  );
}

/**
 * A role picker offering only the roles up to `ceiling`: a token can never
 * outrank the person who makes it. A current role above the ceiling still
 * shows, as a choice that cannot be picked.
 */
export function RoleSelect({
  label,
  value,
  ceiling,
  disabled = false,
  onChange,
}: {
  label: string;
  value: Role;
  ceiling: string;
  disabled?: boolean;
  onChange: (role: Role) => void;
}) {
  const offered = rolesUpTo(ceiling);
  return (
    <select
      className="field st-role"
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as Role)}
    >
      {offered.includes(value) ? null : (
        <option value={value} disabled>
          {value}
        </option>
      )}
      {offered.map((role) => (
        <option key={role} value={role}>
          {role}
        </option>
      ))}
    </select>
  );
}

/**
 * A row's role, changed on a pick: the picked role shows while it saves, then
 * the refetched one. The write's state shows under the picker.
 */
export function RoleCell({
  label,
  role,
  ceiling,
  disabled,
  writer,
  edit,
}: {
  label: string;
  role: Role;
  ceiling: string;
  disabled: boolean;
  writer: AccountWriter;
  edit: (role: Role) => Edit<unknown>;
}) {
  const [picked, setPicked] = useState<Role | null>(null);
  const change = async (next: Role) => {
    setPicked(next);
    await writer.run(edit(next));
    setPicked(null);
  };
  return (
    <div className="st-cell">
      <RoleSelect
        label={label}
        value={picked ?? role}
        ceiling={ceiling}
        disabled={disabled || picked !== null}
        onChange={(next) => void change(next)}
      />
      <WriteStatus state={writer.state} />
    </div>
  );
}

/**
 * Ask before a write that cannot be undone. The dialog stays open while it
 * saves and when it fails, with the write's state; it closes once it lands.
 * Cancel, the default, and Escape close it without writing, but not while it
 * saves: the write goes on either way, and its state would go with the dialog.
 */
export function ConfirmDialog({
  title,
  confirm,
  writer,
  disabled,
  onConfirm,
  onClose,
  children,
}: {
  title: string;
  /** The confirm button's label: `Revoke`, `Delete user`. */
  confirm: string;
  writer: AccountWriter;
  disabled: boolean;
  onConfirm: () => Promise<boolean>;
  onClose: () => void;
  children: ReactNode;
}) {
  const pending = unsettled(writer.state);
  return (
    <AlertDialog.Root open onOpenChange={(open) => open || pending || onClose()}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="modal-backdrop" />
        <AlertDialog.Content className="modal small">
          <div className="modal-b">
            <AlertDialog.Title asChild>
              <h3>{title}</h3>
            </AlertDialog.Title>
            <AlertDialog.Description asChild>
              <p>{children}</p>
            </AlertDialog.Description>
            <WriteStatus state={writer.state} />
          </div>
          <div className="modal-f">
            <div className="spacer" />
            <AlertDialog.Cancel className="btn ghost" disabled={pending}>
              Cancel
            </AlertDialog.Cancel>
            <WriteButton
              className="btn danger"
              pending={pending}
              disabled={disabled}
              onClick={() => void onConfirm().then((landed) => landed && onClose())}
            >
              {confirm}
            </WriteButton>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
