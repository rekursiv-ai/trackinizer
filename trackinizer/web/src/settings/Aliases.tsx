import { useQuery } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { useMeta, useProfile } from "../app/boot";
import { CopyDetails } from "../debug/CopyDetails";
import { useBrowserState } from "../state/store";
import { Icon } from "../ui/icons";
import { ReadError, Section } from "./account";
import { settingsQueries } from "./queries";

/** Owner names offered before "Show all": a long tail of agents' handles follows the most frequent. */
const SHOWN = 8;

/**
 * The names that mean you: your email, and the aliases you pick. Owner and
 * subscriber filters for "me" match any of them. The owner names on rows under
 * your account are offered to tick, most frequent first; nothing is added
 * until you tick it, since the most frequent are often agents' handles. The
 * first 8 show, and what is typed to add a name narrows them to the names
 * holding it, so a name past the first 8 is found without "Show all".
 */
export function AliasesSection() {
  const { email } = useProfile();
  const { kinds } = useMeta();
  const [{ aliases }, update] = useBrowserState();
  const suggested = useQuery(settingsQueries.ownerNames(email, kinds));
  const [typed, setTyped] = useState("");
  const [all, setAll] = useState(false);
  const query = typed.trim().toLowerCase();
  const matches = (suggested.data ?? []).filter(({ name }) => name.toLowerCase().includes(query));
  const shown = all || query ? matches : matches.slice(0, SHOWN);
  const [failure, setFailure] = useState<{ readonly message: string; readonly error: unknown } | null>(null);
  const change = (next: (aliases: readonly string[]) => readonly string[]) => {
    try {
      update((state) => ({ ...state, aliases: next(state.aliases) }));
      setFailure(null);
      return true;
    } catch (error) {
      setFailure({ message: `Not saved: ${error instanceof Error ? error.message : String(error)}`, error });
      return false;
    }
  };
  const add = (name: string) => change((names) => (names.includes(name) || name === email ? names : [...names, name]));
  const remove = (name: string) => change((names) => names.filter((alias) => alias !== name));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const name = typed.trim();
    if (name && add(name)) setTyped("");
  };
  return (
    <Section title="Names that mean you">
      <p className="st-note">Filters for “me” match your email and these names. Assigning or subscribing “me” writes your email.</p>
      <ul className="st-chips" aria-label="Your names">
        <li className="st-chip">
          {email}
          <span className="muted">account</span>
        </li>
        {aliases.map((alias) => (
          <li key={alias} className="st-chip">
            {alias}
            <button type="button" className="st-chip-x" aria-label={`Remove ${alias}`} onClick={() => remove(alias)}>
              <Icon name="x" size={12} />
            </button>
          </li>
        ))}
      </ul>
      <form className="st-form" aria-label="Add a name" onSubmit={submit}>
        <input
          className="field"
          aria-label="Name"
          placeholder="Add a name, or find one of the owner names below"
          autoComplete="off"
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
        />
        <button type="submit" className="btn">
          Add
        </button>
      </form>
      {failure ? (
        <p className="form-err" role="alert">
          {failure.message}
          <CopyDetails message={failure.message} error={failure.error} />
        </p>
      ) : null}
      <h4 className="st-sub">Owner names on rows under your account</h4>
      <ReadError read={suggested} />
      {suggested.data === undefined ? (
        suggested.isError ? null : <p className="st-note">Looking through rows under your account…</p>
      ) : suggested.data.length === 0 ? (
        <p className="st-note">No other owner names on rows under your account.</p>
      ) : matches.length === 0 ? (
        <p className="st-note">No owner name holds “{typed.trim()}”.</p>
      ) : (
        <>
          <ul className="st-checks" aria-label="Owner names on rows under your account">
            {shown.map(({ name, rows }) => (
              <li key={name}>
                <label>
                  <input
                    type="checkbox"
                    checked={aliases.includes(name)}
                    onChange={(event) => (event.target.checked ? add(name) : remove(name))}
                  />
                  <span title={name}>{name}</span>
                  <span className="muted num">
                    {rows} {rows === 1 ? "row" : "rows"}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {shown.length < matches.length ? (
            <button type="button" className="btn ghost st-more" onClick={() => setAll(true)}>
              Show all {matches.length}
            </button>
          ) : null}
        </>
      )}
    </Section>
  );
}
