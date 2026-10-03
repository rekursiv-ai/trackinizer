import { useState } from "react";
import { Icon } from "../ui/icons";
import { errorDetails, keepRefused } from "./details";

/**
 * Copy details, beside a failure the UI shows: puts `errorDetails(message,
 * error)` on the clipboard, for a bug report, and says how it went. Built when
 * pressed, so the recent events run up to the press.
 *
 * An icon, named by its label, so the failure's line keeps its width and text;
 * `labelled` shows the label too, where there is room (the crash screen). It
 * needs no provider, so the crash screen can show it. A browser that refuses the
 * clipboard (a page served over plain http from another machine) keeps the text
 * for `trackinizer.details()`, and the console says so; the text is not logged,
 * since the message can hold what a user wrote.
 */
export function CopyDetails({ message, error, labelled = false }: { message: string; error: unknown; labelled?: boolean }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  const copy = () => {
    const text = errorDetails(message, error);
    Promise.resolve()
      .then(() => navigator.clipboard.writeText(text))
      .then(
        () => setCopied(true),
        () => {
          keepRefused(text);
          console.warn("Trackinizer web app: the browser refused the clipboard; run trackinizer.details() here for the details.");
          setCopied(false);
        },
      );
  };
  const label = copied === null ? "Copy details" : copied ? "Details copied" : "Not copied: see the console";
  return (
    <button type="button" className="btn ghost" aria-label={labelled ? undefined : label} title={label} onClick={copy}>
      <Icon name={copied === null ? "copy" : copied ? "check" : "x"} size={13} />
      {labelled ? label : null}
    </button>
  );
}
