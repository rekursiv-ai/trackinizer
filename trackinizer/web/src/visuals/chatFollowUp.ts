/**
 * Whether an assistant message reads like a campaign recap that proposes a
 * follow-up experiment, so Chat may offer a one-click "keep going". It is a
 * best-effort read of the words, not a contract the assistant is held to: it
 * fires on a line that opens with "next experiment", "follow-up" or "next
 * step", as a close-out names what it would run next, whether that line is a
 * heading, a bold lead-in, or a plain `Next experiment:`. Inline prose ("the
 * next step is …") does not count: the phrase must start its line.
 */
export function followUpSuggested(text: string): boolean {
  return FOLLOW_UP.test(text);
}

/** The proposed next experiment this browser sends when the follow-up button is pressed. */
export const FOLLOW_UP_ASK = "Yes — go ahead and launch the follow-up experiment you proposed.";

const PHRASE = "next experiments?|follow[\\s-]?ups?|next steps?";
const FOLLOW_UP = new RegExp(
  // A markdown heading or bold lead-in naming the proposal, or a plain line that leads with it then a colon or dash.
  `^\\s{0,3}(?:#{1,6}\\s+|\\*\\*\\s*|__\\s*)(?:${PHRASE})\\b|^\\s{0,3}(?:next experiments?|follow[\\s-]?ups?)\\s*[:–—-]`,
  "im",
);
