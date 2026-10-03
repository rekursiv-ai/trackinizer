import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { createToken, type Role, revokeToken, setTokenRole, type Token } from "../api/me";
import { useProfile } from "../app/boot";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { useCopy } from "../ui/toast";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { accountEdit, ConfirmDialog, ReadError, RoleCell, RoleSelect, Section, useAccountWriter } from "./account";
import { settingsQueries } from "./queries";
import { rolesUpTo } from "./roles";

const TOKENS = settingsQueries.tokens.queryKey;

/**
 * The caller's API tokens: make one, with a role up to theirs; change a
 * token's role; revoke one. A new token's secret shows once, right after it is
 * made, and never again: it lives only in this section's state until Done, or
 * until the list shows its token revoked, and never reaches the query cache,
 * browser storage or a log.
 */
export function TokensSection() {
  const tokens = useQuery(settingsQueries.tokens);
  const [making, setMaking] = useState(false);
  const [made, setMade] = useState<{ readonly id: string; readonly secret: string } | null>(null);
  // A revoked token's secret is no use to copy.
  if (made !== null && tokens.data?.some((listed) => listed.id === made.id && listed.revoked_at)) setMade(null);
  const secret = made?.secret ?? null;
  const now = useMinuteClock();
  return (
    <Section
      title="API tokens"
      actions={
        making || secret !== null ? null : (
          <button type="button" className="btn" onClick={() => setMaking(true)}>
            <Icon name="plus" size={13} />
            New token
          </button>
        )
      }
    >
      {secret !== null ? <Secret secret={secret} onDone={() => setMade(null)} /> : null}
      {making ? (
        <NewTokenForm
          onMade={(id, secret) => {
            setMade({ id, secret });
            setMaking(false);
          }}
          onCancel={() => setMaking(false)}
        />
      ) : null}
      <ReadError read={tokens} />
      {tokens.data === undefined ? (
        tokens.isError ? null : <p className="st-note">Loading…</p>
      ) : tokens.data.length === 0 ? (
        <p className="st-note">No tokens yet. A token lets trax and scripts act as you.</p>
      ) : (
        <table className="st-t">
          <thead>
            <tr>
              <th>Label</th>
              <th>Prefix</th>
              <th>Role</th>
              <th>Created</th>
              <th>Last used</th>
              <th>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {tokens.data.map((token) => (
              <TokenRow key={token.id} token={token} now={now} />
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

/** The new token's secret, with Copy, until Done. */
function Secret({ secret, onDone }: { secret: string; onDone: () => void }) {
  const copy = useCopy();
  return (
    <div className="st-secret" role="status">
      <Icon name="check" size={14} />
      <span>Copy this secret now; it won't be shown again:</span>
      <code>{secret}</code>
      <button type="button" className="btn ghost" onClick={() => void copy(secret, "Copied the token's secret")}>
        <Icon name="copy" size={13} />
        Copy
      </button>
      <button type="button" className="btn ghost" onClick={onDone}>
        Done
      </button>
    </div>
  );
}

/**
 * A label and a role, capped at the caller's; Create sends it once, and never
 * again: with no sure answer, the refetched list shows whether it was made.
 * Cancel is off while it is pending, since closing the form would lose the secret.
 */
function NewTokenForm({ onMade, onCancel }: { onMade: (id: string, secret: string) => void; onCancel: () => void }) {
  const ceiling = useProfile().role;
  const online = useOnline();
  const writer = useAccountWriter(TOKENS);
  const [label, setLabel] = useState("");
  // The weaker of writer and the caller's own role: least privilege by default.
  const [role, setRole] = useState<Role>(() => rolesUpTo(ceiling).slice(0, 2).at(-1) ?? "viewer");
  const pending = unsettled(writer.state);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const body = { name: label.trim(), role };
    // The secret goes to `onMade` alone: the write layer's result, which its
    // mutation cache keeps for minutes, is the token without it. The cache
    // keeps `send` too, as the request, so `made` is emptied once read.
    const made: { id?: string; secret?: string } = {};
    const send = async () => {
      const { secret, ...token } = await createToken(body);
      Object.assign(made, { id: token.id, secret });
      return token;
    };
    const landed = await writer.run({ ...accountEdit(send), maybeLanded: MAYBE_MADE });
    const { id, secret } = made;
    delete made.secret;
    if (landed && id !== undefined && secret !== undefined) onMade(id, secret);
  };
  return (
    <form className="st-form" aria-label="New token" onSubmit={(event) => void submit(event)}>
      <input
        className="field"
        aria-label="Label"
        placeholder="Label, e.g. laptop trax"
        autoComplete="off"
        autoFocus
        value={label}
        onChange={(event) => setLabel(event.target.value)}
      />
      <RoleSelect label="Role" value={role} ceiling={ceiling} onChange={setRole} />
      <button type="submit" className="btn primary" disabled={!online} aria-disabled={pending || undefined}>
        Create token
      </button>
      <button type="button" className="btn ghost" onClick={onCancel} disabled={pending}>
        Cancel
      </button>
      <WriteStatus state={writer.state} />
    </form>
  );
}

const MAYBE_MADE =
  "The server did not confirm the token, so it may have been made anyway: the list below shows whether it was. Its secret cannot be shown again, so revoke it there if it was.";

function TokenRow({ token, now }: { token: Token; now: number }) {
  const ceiling = useProfile().role;
  const online = useOnline();
  const roleWriter = useAccountWriter(TOKENS);
  const revokeWriter = useAccountWriter(TOKENS);
  const [confirming, setConfirming] = useState(false);
  const { name, revoked_at: revoked } = token;
  return (
    <tr className={revoked ? "dim" : undefined}>
      <td>{name}</td>
      <td className="mono">{token.prefix}…</td>
      <td>
        {revoked ? (
          token.role
        ) : (
          <RoleCell
            label={`Role of ${name}`}
            role={token.role}
            ceiling={ceiling}
            disabled={!online}
            writer={roleWriter}
            edit={(role) => accountEdit(() => setTokenRole(token.id, role), `${name}: role set to ${role}`)}
          />
        )}
      </td>
      <td title={dateTime(token.created_at)}>{relativeTime(token.created_at, now)}</td>
      <td title={token.last_used_at ? dateTime(token.last_used_at) : undefined}>
        {token.last_used_at ? relativeTime(token.last_used_at, now) : "never"}
      </td>
      <td className="acts">
        {revoked ? (
          <span className="muted" title={dateTime(revoked)}>
            revoked {relativeTime(revoked, now)}
          </span>
        ) : (
          <button
            type="button"
            className="btn ghost danger"
            disabled={!online}
            aria-label={`Revoke ${name}`}
            onClick={() => setConfirming(true)}
          >
            Revoke
          </button>
        )}
        {confirming ? (
          <ConfirmDialog
            title={`Revoke “${name}”?`}
            confirm="Revoke"
            writer={revokeWriter}
            disabled={!online}
            onConfirm={() => revokeWriter.run(accountEdit(() => revokeToken(token.id), `Revoked “${name}”`))}
            onClose={() => setConfirming(false)}
          >
            Anything using the token {token.prefix}… stops working at once. A revoked token cannot be restored; make a
            new one instead.
          </ConfirmDialog>
        ) : null}
      </td>
    </tr>
  );
}
