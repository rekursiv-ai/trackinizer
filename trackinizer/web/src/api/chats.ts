// A science chat is the server's assistant's, and this app draws no Chat; its sessions are still records here,
// so the app knows them by the names the server gives them.

/** The label the server puts on a science chat's AgentSession. */
export const SCIENCE_CHAT_LABEL = "science-chat";

/** Whether a session's routing name is the one the assistant opens a science chat under: `chat-` and twelve hex digits. */
export function isChatHandle(actor: string): boolean {
  return /^chat-[0-9a-f]{12}$/.test(actor);
}
