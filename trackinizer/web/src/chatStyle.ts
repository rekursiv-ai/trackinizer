import { choosePreferences, preferences, subscribePreferences, usePreferences } from "./preferences";

export type ChatStyle = "default" | "terminal";

/** The user's Chat style (Default until the profile brings theirs). */
export function readChatStyle(): ChatStyle {
  return preferences().chat_style;
}

/** Mark the page with the style, which the Chat panel's stylesheet selects on; Default leaves it unmarked. */
export function applyChatStyle(choice: ChatStyle): void {
  if (choice === "terminal") document.documentElement.dataset.chatStyle = "terminal";
  else delete document.documentElement.dataset.chatStyle;
}

/** Apply a choice at once and keep it in the user's preferences. */
export function chooseChatStyle(choice: ChatStyle): void {
  applyChatStyle(choice);
  void choosePreferences({ chat_style: choice });
}

/** The style chosen, kept current wherever it is chosen. */
export function useChatStyle(): ChatStyle {
  return usePreferences().chat_style;
}

/** Show the style, and every change to it, chosen here or read from the server. */
export function installChatStyle(): void {
  applyChatStyle(readChatStyle());
  subscribePreferences(() => applyChatStyle(readChatStyle()));
}
