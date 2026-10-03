import { CopyDetails } from "../debug/CopyDetails";

/**
 * A read that failed, in the place of what it would have shown: the server's
 * message, Retry and Copy details.
 *
 * Only a read with nothing to show fails this way. One that still holds data
 * keeps showing it, and says it could not refresh beside it: in a `Bar` for a
 * view, in its own header line for a section of one. A failure never takes the
 * place of data it could not refresh, and never moves it.
 */
export function ReadFailure({ error, retry }: { error: Error; retry: () => void }) {
  return (
    <p className="read-error" role="alert">
      {error.message}
      <button type="button" className="btn" onClick={retry}>
        Retry
      </button>
      <CopyDetails message={error.message} error={error} />
    </p>
  );
}
