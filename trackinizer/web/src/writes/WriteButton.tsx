import type { ComponentProps } from "react";
import "./writes.css";

/**
 * A button that makes a write, or opens what makes one, for a control whose
 * write may be `pending` (`unsettled`: sent, or failed and showing Retry and
 * Discard). While it is, the button reads as off and does
 * nothing, since the writer ignores a second edit: preventing the click also
 * keeps a Radix trigger shut and a submit button from submitting.
 *
 * Not `disabled` while pending: a closing menu or dialog hands focus back to
 * its button, and a disabled one would drop it. Offline, pass `disabled`.
 */
export function WriteButton({ pending, onClick, ...props }: ComponentProps<"button"> & { pending: boolean }) {
  return (
    <button
      type="button"
      {...props}
      aria-disabled={pending || undefined}
      onClick={(event) => {
        if (pending) event.preventDefault();
        else onClick?.(event);
      }}
    />
  );
}
