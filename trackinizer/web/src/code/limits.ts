// The code store's bounds, mirrored from `trackinizer/wire/wire_code.py`.
//
// The uploader must know these BEFORE it sends: a request over any of them is a 422, and
// the point of the import is to split the work so none is ever exceeded. They are plain
// module constants in the wire package, not part of the OpenAPI schema, so codegen cannot
// reach them — `code_limits_test.py` fails if these drift from the Python.
/** Largest blob, in UTF-8 bytes. */
export const MAX_BLOB_BYTES = 1_048_576;

/** Most UTF-8 bytes of text one blobs request carries. */
export const MAX_REQUEST_TEXT_BYTES = 8 * MAX_BLOB_BYTES;

/** Most blobs one blobs request carries. */
export const MAX_BLOBS_PER_REQUEST = 1_000;

/** Most hashes one missing-blobs request asks about. */
export const MAX_MISSING_HASHES = 5_000;

/** Most files one snapshot maps. */
export const MAX_SNAPSHOT_FILES = 20_000;

/** Longest path in a snapshot. */
export const MAX_PATH_CHARS = 1_024;
