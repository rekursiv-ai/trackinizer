"""Domain exceptions surfaced to HTTP callers."""

from __future__ import annotations


class ConflictError(Exception):
    """A requested state transition clashes with the current state.

    The API's ``conflict_handler`` turns this into an HTTP 409.
    """

    code = "conflict"


class NotFoundError(ConflictError):
    """A mutation targeted an id that does not exist.

    A subclass of :class:`ConflictError` so the existing not-found store
    paths can raise it without a new control-flow branch, but the API's
    ``not_found_handler`` turns it into an HTTP 404 (the more specific
    handler wins over the 409 ``conflict_handler``). Distinguishing 404
    from 409 lets a client tell "the row is gone" from "the row exists but
    the requested transition clashes".
    """

    code = "not_found"


class ValidationError(Exception):
    """A request is malformed -- semantically invalid on its own terms.

    Distinct from :class:`ConflictError`: a ``ValidationError`` is knowable
    from the request alone, without consulting stored state (e.g. a priority
    on an edge kind that has none, or a self-loop). The API's
    ``validation_handler`` turns it into an HTTP 422 (Unprocessable Content),
    per RFC 9110, whereas a ``ConflictError`` -- a clash with existing state,
    such as a cycle -- stays 409.

    NOT a subclass of ``ConflictError``: that would route it through the 409
    handler. It is a sibling so the 422 handler claims it.
    """

    code = "validation"
