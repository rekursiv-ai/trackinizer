import type { BrowserState, Person } from "../state/value";

/** People and agents added by hand in this browser, by the value written for each. */
export type People = BrowserState["people"];

/** What the New person dialog adds: a person, or an agent. */
export type Who = Person["type"];

/** The New person dialog's fields, as typed. */
export type PersonDraft = { readonly who: Who; readonly name: string; readonly email: string; readonly handle: string };

/** A person or an agent as a picker offers it: the value written, the name shown, and a hint. */
export type Offered = { readonly actor: string; readonly label: string; readonly hint?: string };

/**
 * The value a new person or agent is written as, and what this browser keeps
 * to name it by; or why it cannot be added.
 *
 * A person is written as their email when they have one, since that ties to
 * their account, and otherwise as their name; an agent as its handle, which
 * `trax run --as` sessions are owned by and pushed to. A display name is never
 * written. An email is lowercased: the allowlist stores it so, and accounts
 * are made from it.
 */
export function newActor(draft: PersonDraft): { readonly actor: string; readonly person: Person } | { readonly error: string } {
  if (draft.who === "agent") {
    const handle = draft.handle.trim();
    if (!/^\S+$/.test(handle)) return { error: "Give the agent a one-word handle, such as craftax-arm." };
    return { actor: handle, person: { name: handle, type: "agent" } };
  }
  const name = draft.name.trim();
  const email = draft.email.trim().toLowerCase();
  if (!name && !email) return { error: "Add a name or an email." };
  if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return { error: "Enter an email such as jane@example.com, or leave it empty." };
  return { actor: email || name, person: { name: name || email, type: "person" } };
}

/**
 * The dialog's fields for text typed into a picker: an email or a spaced name
 * reads as a person, and one word with a dash, underscore, dot, colon or digit
 * as an agent's handle, as the mock guesses.
 */
export function draftFor(typed: string, who?: Who): PersonDraft {
  const text = typed.trim();
  const guessed = who ?? (/[@\s]/.test(text) ? "person" : /[-_.:\d]/.test(text) ? "agent" : "person");
  if (guessed === "agent") return { who: guessed, name: "", email: "", handle: text };
  const email = text.includes("@");
  return { who: guessed, name: email ? "" : text, email: email ? text : "", handle: "" };
}

/** The name `actor` is shown by: the one added by hand, or the value itself. */
export function actorName(actor: string, people: People): string {
  return people[actor]?.name ?? actor;
}

/**
 * Everyone a picker offers, each once: me first, then the rest by name. The
 * hint says "me", "agent", or the value written when it is not the name shown.
 */
export function offered(me: string, actors: Iterable<string>, people: People): Offered[] {
  const [first, ...rest] = [...new Set([me, ...actors])].map((actor): Offered => {
    const label = actorName(actor, people);
    const hint = actor === me ? "me" : people[actor]?.type === "agent" ? "agent" : label === actor ? undefined : actor;
    return { actor, label, ...(hint !== undefined && { hint }) };
  });
  return [first!, ...rest.sort((a, b) => a.label.localeCompare(b.label) || a.actor.localeCompare(b.actor))];
}
