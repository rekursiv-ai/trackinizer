import { useMemo } from "react";
import { useProfile } from "../app/boot";
import { escapeRegex, type Filter } from "../query/query";
import { useBrowserState } from "./store";

/**
 * Every name that means me: the account email, then the aliases ticked in
 * Settings, each once. `owner` and `subscribers` are free text, and most rows
 * name their owner by a handle rather than the email, so a filter on the email
 * alone would miss that work.
 */
export function meNames(email: string, aliases: readonly string[]): string[] {
  return [...new Set([email, ...aliases])];
}

/** One anchored regex matching exactly any of `names`, each regex-escaped. */
export function mePattern(names: readonly string[]): string {
  return `^(${names.map(escapeRegex).join("|")})$`;
}

/**
 * A filter matching rows whose `field` (`owner`, `subscribers`) is me, under
 * any of my names. Writing "me" writes the email, which ties to the account.
 */
export function meFilter(field: string, names: readonly string[]): Filter {
  return { field, op: "re", value: mePattern(names) };
}

/** The signed-in user's names, for `meFilter`. */
export function useMe(): readonly string[] {
  const { email } = useProfile();
  const [{ aliases }] = useBrowserState();
  return useMemo(() => meNames(email, aliases), [email, aliases]);
}
