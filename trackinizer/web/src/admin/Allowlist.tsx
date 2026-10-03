import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { type AllowlistEntry, addAllowlistEntry, removeAllowlistEntry, setAllowlistRole } from "../api/admin";
import type { Role } from "../api/me";
import { useProfile } from "../app/boot";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { accountEdit, ConfirmDialog, ReadError, RoleCell, RoleSelect, Section, useAccountWriter } from "../settings/account";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { useToast } from "../ui/toast";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { adminQueries } from "./queries";

const ALLOWLIST = adminQueries.allowlist.queryKey;

/**
 * Who may sign in: addresses and domain wildcards, each with the role a user
 * gets on their first sign-in through it. Removing one cannot be undone, so it
 * asks first.
 */
export function AllowlistSection() {
  const entries = useQuery(adminQueries.allowlist);
  const now = useMinuteClock();
  return (
    <Section title="Allowlist">
      <AddEntryForm />
      <p className="st-note">
        An entry's role applies to a user's first sign-in. After that, change the user's role above.
      </p>
      <ReadError read={entries} />
      {entries.data === undefined ? (
        entries.isError ? null : <p className="st-note">Loading…</p>
      ) : entries.data.length === 0 ? (
        <p className="st-note">Nobody new can sign in: the allowlist is empty.</p>
      ) : (
        <table className="st-t">
          <thead>
            <tr>
              <th>Email or pattern</th>
              <th>Role</th>
              <th>Added</th>
              <th>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {entries.data.map((entry) => (
              <EntryRow key={entry.email_or_pattern} entry={entry} now={now} />
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

/**
 * An address or a wildcard and its role; the server trims it, lowercases it,
 * and refuses a duplicate. Both stay as sent while the add is pending, and
 * after it fails until Discard, so a landed add clears what it sent and Retry
 * sends what the form shows.
 */
function AddEntryForm() {
  const ceiling = useProfile().role;
  const online = useOnline();
  const writer = useAccountWriter(ALLOWLIST);
  const toast = useToast();
  const [entry, setEntry] = useState("");
  const [role, setRole] = useState<Role>("writer");
  const sent = unsettled(writer.state);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (sent) return;
    // Sent as typed: the server trims and lowercases it, and refuses a blank one.
    const added: { entry?: string } = {};
    const send = async () => {
      const stored = await addAllowlistEntry({ email_or_pattern: entry, role });
      added.entry = stored.email_or_pattern;
      return stored;
    };
    if (await writer.run(accountEdit(send))) {
      setEntry("");
      toast(`Added ${added.entry} as ${role}`);
    }
  };
  return (
    <form className="st-form" aria-label="Add to the allowlist" onSubmit={(event) => void submit(event)}>
      <input
        className="field"
        aria-label="Email or pattern"
        placeholder="name@example.com or *@example.com"
        autoComplete="off"
        readOnly={sent}
        value={entry}
        onChange={(event) => setEntry(event.target.value)}
      />
      <RoleSelect label="Role for new entry" value={role} ceiling={ceiling} disabled={sent} onChange={setRole} />
      <WriteButton type="submit" className="btn primary" pending={sent} disabled={!online}>
        Add
      </WriteButton>
      <WriteStatus state={writer.state} />
    </form>
  );
}

function EntryRow({ entry, now }: { entry: AllowlistEntry; now: number }) {
  const ceiling = useProfile().role;
  const online = useOnline();
  const roleWriter = useAccountWriter(ALLOWLIST);
  const removeWriter = useAccountWriter(ALLOWLIST);
  const [confirming, setConfirming] = useState(false);
  const name = entry.email_or_pattern;
  return (
    <tr>
      <td className="mono">{name}</td>
      <td>
        <RoleCell
          label={`Role for ${name}`}
          role={entry.role}
          ceiling={ceiling}
          disabled={!online}
          writer={roleWriter}
          edit={(role) => accountEdit(() => setAllowlistRole(name, role), `${name}: role set to ${role}`)}
        />
      </td>
      <td title={dateTime(entry.added_at)}>{relativeTime(entry.added_at, now)}</td>
      <td className="acts">
        <button
          type="button"
          className="icon-btn"
          disabled={!online}
          title="Remove"
          aria-label={`Remove ${name}`}
          onClick={() => setConfirming(true)}
        >
          <Icon name="x" size={13} />
        </button>
        {confirming ? (
          <ConfirmDialog
            title={`Remove ${name}?`}
            confirm="Remove"
            writer={removeWriter}
            disabled={!online}
            onConfirm={() => removeWriter.run(accountEdit(() => removeAllowlistEntry(name), `Removed ${name}`))}
            onClose={() => setConfirming(false)}
          >
            Nobody new can sign in through it. Users it already let in keep their accounts, but their next sign-in is refused
            unless another entry matches them.
          </ConfirmDialog>
        ) : null}
      </td>
    </tr>
  );
}
