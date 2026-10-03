import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { deleteUser, disableUser, enableUser, setUserRole, type User } from "../api/admin";
import { useProfile } from "../app/boot";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { accountEdit, ConfirmDialog, ReadError, RoleCell, Section, useAccountWriter } from "../settings/account";
import { useOnline } from "../ui/bars";
import { Avatar } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { adminQueries } from "./queries";

const USERS = adminQueries.users.queryKey;

/**
 * Every user: change a role, disable or enable, delete. Your own row has no
 * controls: the server refuses an admin's own demotion, disabling and
 * deletion, so the org cannot lock itself out. Disabling revokes every token
 * the user holds for good, and deleting cannot be undone, so both ask first.
 */
export function UsersSection() {
  const users = useQuery(adminQueries.users);
  const now = useMinuteClock();
  return (
    <Section title="Users">
      <ReadError read={users} />
      {users.data === undefined ? (
        users.isError ? null : <p className="st-note">Loading…</p>
      ) : (
        <table className="st-t">
          <thead>
            <tr>
              <th>Name</th>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Created</th>
              <th>Last sign-in</th>
              <th>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {users.data.map((user) => (
              <UserRow key={user.id} user={user} now={now} />
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

function UserRow({ user, now }: { user: User; now: number }) {
  const me = useProfile();
  const online = useOnline();
  const roleWriter = useAccountWriter(USERS);
  const statusWriter = useAccountWriter(USERS);
  const deleteWriter = useAccountWriter(USERS);
  const [asking, setAsking] = useState<"disable" | "delete" | null>(null);
  const self = user.id === me.user_id;
  const active = user.status === "active";
  const who = user.name || user.email;
  return (
    <tr className={active ? undefined : "dim"}>
      <td>
        <span className="st-who">
          <Avatar actor={user.email} size={18} />
          {user.name}
        </span>
      </td>
      <td className="mono">{user.email}</td>
      <td>
        {self ? (
          user.role
        ) : (
          <RoleCell
            label={`Role of ${user.email}`}
            role={user.role}
            ceiling={me.role}
            disabled={!online}
            writer={roleWriter}
            edit={(role) => accountEdit(() => setUserRole(user.id, role), `${who}: role set to ${role}`)}
          />
        )}
      </td>
      <td>{user.status}</td>
      <td title={dateTime(user.created_at)}>{relativeTime(user.created_at, now)}</td>
      <td title={user.last_login ? dateTime(user.last_login) : undefined}>
        {user.last_login ? relativeTime(user.last_login, now) : "never"}
      </td>
      <td className="acts">
        {self ? (
          <span className="muted">you</span>
        ) : (
          <>
            {active ? (
              <button type="button" className="btn ghost" disabled={!online} aria-label={`Disable ${user.email}`} onClick={() => setAsking("disable")}>
                Disable
              </button>
            ) : (
              <button
                type="button"
                className="btn ghost"
                disabled={!online}
                aria-label={`Enable ${user.email}`}
                aria-disabled={unsettled(statusWriter.state) || undefined}
                onClick={() => void statusWriter.run(accountEdit(() => enableUser(user.id), `Enabled ${who}`))}
              >
                Enable
              </button>
            )}
            <button
              type="button"
              className="icon-btn"
              disabled={!online}
              title="Delete user"
              aria-label={`Delete ${user.email}`}
              onClick={() => setAsking("delete")}
            >
              <Icon name="trash" size={13} />
            </button>
            {asking === null ? <WriteStatus state={statusWriter.state} /> : null}
          </>
        )}
        {asking === "disable" ? (
          <ConfirmDialog
            title={`Disable ${who}?`}
            confirm="Disable"
            writer={statusWriter}
            disabled={!online}
            onConfirm={() => statusWriter.run(accountEdit(() => disableUser(user.id), `Disabled ${who}`))}
            onClose={() => setAsking(null)}
          >
            {user.email} can no longer sign in, and every token they hold is revoked. Enabling them again lets them sign in, but
            does not bring the tokens back.
          </ConfirmDialog>
        ) : null}
        {asking === "delete" ? (
          <ConfirmDialog
            title={`Delete ${who}?`}
            confirm="Delete user"
            writer={deleteWriter}
            disabled={!online}
            onConfirm={() => deleteWriter.run(accountEdit(() => deleteUser(user.id), `Deleted ${who}`))}
            onClose={() => setAsking(null)}
          >
            {user.email} and their tokens are deleted, which cannot be undone. Changes they made keep their name. If the
            allowlist still lets them in, their next sign-in makes a new user.
          </ConfirmDialog>
        ) : null}
      </td>
    </tr>
  );
}
