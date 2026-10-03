import { Toast } from "radix-ui";
import { createContext, type ReactNode, useCallback, useContext, useRef, useState } from "react";
import { CopyDetails } from "../debug/CopyDetails";
import { Icon } from "./icons";

/**
 * How a toast shows: Retry for a failure, Undo for a change that has an exact
 * inverse, and whether it reports a failure. A failure has a cross where a
 * success has a check, and offers Copy details, of `error` when given; a toast
 * with Retry is always one.
 */
export type ToastOptions = {
  readonly retry?: () => void;
  readonly undo?: () => void;
  readonly failed?: boolean;
  readonly error?: unknown;
};

/** Show a short message, with Retry or Undo when given. */
export type ShowToast = (message: string, options?: ToastOptions) => void;

const ToastContext = createContext<ShowToast | null>(null);

/**
 * Hold the toasts, over Radix Toast.
 *
 * Radix gives the live region, pause on hover and focus, swipe to dismiss, and
 * F8 to reach the toasts from the keyboard. Each toast closes after 5 s, as in
 * the mock, or when its action is pressed.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<({ id: number; message: string } & ToastOptions)[]>([]);
  const nextId = useRef(0);
  const show = useCallback<ShowToast>((message, options = {}) => {
    const id = nextId.current++;
    setToasts((shown) => [...shown, { id, message, ...options }]);
  }, []);
  return (
    <Toast.Provider duration={5000} label="Notifications">
      <ToastContext value={show}>{children}</ToastContext>
      {toasts.map(({ id, message, retry, undo, failed = retry !== undefined, error = null }) => (
        <Toast.Root
          key={id}
          className="toast"
          onOpenChange={(open) => {
            if (!open) setToasts((shown) => shown.filter((toast) => toast.id !== id));
          }}
        >
          <Icon name={failed ? "x" : "check"} size={15} className={failed ? "failed" : ""} />
          <Toast.Description className="toast-text">{message}</Toast.Description>
          {retry && (
            <Toast.Action altText="Retry" onClick={retry}>
              Retry
            </Toast.Action>
          )}
          {/* Not a Toast.Action, which would close the toast and the button's answer with it. */}
          {failed && <CopyDetails message={message} error={error} />}
          {undo && (
            <Toast.Action altText="Undo" onClick={undo}>
              Undo
            </Toast.Action>
          )}
        </Toast.Root>
      ))}
      <Toast.Viewport className="toast-viewport" />
    </Toast.Provider>
  );
}

/** The function that shows a toast. */
export function useToast(): ShowToast {
  const show = useContext(ToastContext);
  if (!show) throw new Error("useToast needs a ToastProvider above it.");
  return show;
}

/**
 * Copy text to the clipboard and say how it went: `done` when it worked, a
 * failure toast when the browser refused, or has no clipboard to offer (a page
 * served over plain http from anywhere but this machine).
 */
export function useCopy(): (text: string, done: string) => Promise<void> {
  const toast = useToast();
  return useCallback(
    (text, done) =>
      Promise.resolve()
        .then(() => navigator.clipboard.writeText(text))
        .then(
          () => toast(done),
          () => toast("Could not copy: the browser refused the clipboard.", { failed: true }),
        ),
    [toast],
  );
}
