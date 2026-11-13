/**
 * `value` itself when it is neither null nor undefined; otherwise a thrown error.
 *
 * The checked twin of TypeScript's `value!`, which is erased at compile time and
 * checks nothing: a missing value fails here, where it was assumed present.
 */
export function must<T>(value: T): NonNullable<T> {
  if (value === null || value === undefined) {
    throw new TypeError(`Expected a value, got ${value}`);
  }
  return value;
}
