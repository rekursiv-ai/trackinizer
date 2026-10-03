import { useQuery } from "@tanstack/react-query";
import { Dialog } from "radix-ui";
import { type KeyboardEvent, useState } from "react";
import { addAllowlistEntry } from "../api/admin";
import { adminQueries } from "../admin/queries";
import { useProfile, useWriteMode } from "../app/boot";
import { keyCaps } from "../commands/registry";
import { accountEdit, useAccountWriter } from "../settings/account";
import { useBrowserState } from "../state/store";
import { Avatar } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { useToast } from "../ui/toast";
import { WriteButton } from "../writes/WriteButton";
import { unsettled, WriteStatus } from "../writes/WriteStatus";
import { newActor, type PersonDraft, type People, type Who } from "./people";
import "../editors/editors.css";
import "../writes/writes.css";

/**
 * The mock's New person dialog: a person, by name and an optional email, or an
 * agent, by its routing handle, added as `role`. Adding keeps their name in
 * this browser (`people` in its state) and hands `onAdd` the value to write
 * (`newActor`). ⌘↵ adds; Escape and Cancel call `onClose`.
 *
 * An admin can also invite a person to sign in: their email goes on the
 * allowlist as a writer, through the write layer, which never resends it on its
 * own. A person is added only once the invite lands; a refused or failed one
 * stays in the dialog, with Retry and Discard for a failure. Admins see whether
 * the email already has an account or an entry, and are then offered no invite.
 */
export function PersonDialog({
  role,
  draft: opened,
  onAdd,
  onClose,
  returnFocus,
}: {
  role: "owner" | "subscriber";
  /** The fields the dialog opens with. */
  draft: PersonDraft;
  onAdd: (actor: string) => void;
  onClose: () => void;
  /** Focus the control the dialog was opened from, once it closes. */
  returnFocus: () => void;
}) {
  const admin = useProfile().role === "admin";
  const offline = useWriteMode() === "disabled";
  const [{ people }, update] = useBrowserState();
  const toast = useToast();
  const writer = useAccountWriter(adminQueries.allowlist.queryKey);
  const [draft, setDraft] = useState(opened);
  const [invite, setInvite] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = unsettled(writer.state);
  const email = draft.email.trim().toLowerCase();
  // Only admins can read users and the allowlist.
  const asking = admin && draft.who === "person";
  const users = useQuery({ ...adminQueries.users, enabled: asking });
  const entries = useQuery({ ...adminQueries.allowlist, enabled: asking });
  const user = email ? users.data?.find((listed) => listed.email.toLowerCase() === email) : undefined;
  const entry = email ? entries.data?.find((listed) => listed.email_or_pattern === email) : undefined;
  const invites = asking && !user && !entry;
  const edit = (change: Partial<PersonDraft>) => {
    setDraft({ ...draft, ...change });
    setError(null);
  };

  const save = async () => {
    if (pending || offline) return;
    const made = newActor(draft);
    if ("error" in made) return setError(made.error);
    if (invites && invite) {
      if (!email) return setError("Add the email to invite them.");
      const invitation = accountEdit(() => addAllowlistEntry({ email_or_pattern: email, role: "writer" }), `Invited ${email} to sign in as a writer`);
      if (!(await writer.run(invitation))) return;
    }
    try {
      update((state) => ({ ...state, people: { ...state.people, [made.actor]: made.person } }));
    } catch (failure) {
      // The value is still written; only its name is not kept here.
      toast(`${made.person.name} is not kept in this browser: ${failure instanceof Error ? failure.message : String(failure)}`, { failed: true, error: failure });
    }
    onAdd(made.actor);
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key !== "Enter") return;
    // While an input method composes, Enter is its own: it commits the text.
    if (event.nativeEvent.isComposing) return event.preventDefault();
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault();
      void save();
    }
  };

  return (
    <Dialog.Root open onOpenChange={(open) => open || onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-backdrop" />
        <Dialog.Content
          className="modal small"
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            returnFocus();
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
            onKeyDown={onKeyDown}
          >
            <div className="modal-b">
              <Dialog.Title asChild>
                <h3>New {role}</h3>
              </Dialog.Title>
              <div className="who-seg" role="group" aria-label="Kind">
                {WHO.map(({ who, label, icon }) => (
                  <button
                    key={who}
                    type="button"
                    aria-pressed={draft.who === who}
                    disabled={pending}
                    onClick={() => edit({ who })}
                  >
                    <Icon name={icon} size={13} />
                    {label}
                  </button>
                ))}
              </div>
              <div className="np-fields">
                {draft.who === "agent" ? (
                  <label>
                    Handle
                    <input
                      className="field"
                      autoFocus
                      autoComplete="off"
                      placeholder="craftax-arm"
                      readOnly={pending}
                      value={draft.handle}
                      onChange={(event) => edit({ handle: event.target.value })}
                    />
                  </label>
                ) : (
                  <>
                    <label>
                      Name
                      <input
                        className="field"
                        autoFocus={!opened.name}
                        autoComplete="off"
                        placeholder="Jane Doe"
                        readOnly={pending}
                        value={draft.name}
                        onChange={(event) => edit({ name: event.target.value })}
                      />
                    </label>
                    <label>
                      Email (optional)
                      <input
                        className="field"
                        inputMode="email"
                        autoFocus={Boolean(opened.name)}
                        autoComplete="off"
                        placeholder="jane@example.com"
                        readOnly={pending}
                        value={draft.email}
                        onChange={(event) => edit({ email: event.target.value })}
                      />
                    </label>
                  </>
                )}
              </div>
              <div className="np-hint">
                {draft.who === "agent" ? (
                  <AgentHint handle={draft.handle.trim()} people={people} />
                ) : user ? (
                  `Matches ${user.name || user.email}'s account (${user.role})`
                ) : entry ? (
                  `On the allowlist as ${entry.role}; not signed in yet`
                ) : invites && email && users.data && entries.data ? (
                  "No account yet"
                ) : null}
              </div>
              {invites ? (
                <label className="np-check">
                  <input type="checkbox" checked={invite} disabled={pending} onChange={(event) => setInvite(event.target.checked)} />
                  Invite to sign in: add the email to the allowlist as a writer
                </label>
              ) : null}
              {error ? (
                <div className="form-err" role="alert">
                  {error}
                </div>
              ) : null}
              <WriteStatus state={writer.state} />
            </div>
            <div className="modal-f">
              <div className="spacer" />
              <Dialog.Close className="btn ghost">Cancel</Dialog.Close>
              <WriteButton type="submit" className="btn primary" pending={pending} disabled={offline}>
                Add as {role} <kbd>{keyCaps("$mod+Enter")[0]}</kbd>
              </WriteButton>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Whether a handle is one word, and whether this browser has added that agent already. */
function AgentHint({ handle, people }: { handle: string; people: People }) {
  if (!handle) return null;
  if (/\s/.test(handle)) return "A handle is one word, with no spaces.";
  return (
    <>
      <Avatar actor={handle} size={16} />
      {people[handle]?.type === "agent" ? "Known agent" : "New agent"}
    </>
  );
}

const WHO: readonly { readonly who: Who; readonly label: string; readonly icon: "user" | "bot" }[] = [
  { who: "person", label: "Person", icon: "user" },
  { who: "agent", label: "Agent", icon: "bot" },
];
