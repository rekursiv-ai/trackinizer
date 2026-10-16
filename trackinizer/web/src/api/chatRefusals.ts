import { ApiError } from "./client";
import { CHAT_STAFF_ONLY } from "./chats";
import { SESSION_NOT_OWNER, SESSION_STAFF } from "./sessions";

/** What the server's refusal codes say to the person, for sharing, deleting, or changing a chat's audience. */
const REFUSALS: { readonly [code: string]: string } = {
  chat_not_starter: "Only the person who started this chat can do that.",
  chat_has_other_posters: "Others have posted in this chat, so only an admin can delete it.",
  [CHAT_STAFF_ONLY]: "This chat is staff only, so it can be shared only with someone who holds staff.",
  [SESSION_NOT_OWNER]: "Only the person who started this chat can make it public or private.",
  [SESSION_STAFF]: "This chat is staff only, so it is never made public or private.",
};

/** What to say of a refused or failed change: the words for its code, else the server's reason when it gave one. */
export function chatRefusalMessage(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  const known = error.code === null ? undefined : REFUSALS[error.code];
  return known ?? (error.status >= 400 && error.status < 500 && error.detail ? error.detail : fallback);
}
