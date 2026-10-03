/**
 * `url` when it may be a link or image target, else `""`.
 *
 * Exactly three forms pass: `http://`, `https://`, and an in-app route starting
 * with `#/`, which the ref links use. It is an allowlist, so an obfuscated scheme
 * (`JaVaScRiPt:`, an entity-encoded tab) fails like a plain one. The `//` is part
 * of it: a browser reads `https:foo` or `http:/auth/logout` as a path on the
 * app's own origin, since the scheme is the page's. react-markdown's default
 * would also pass `mailto:`, `irc:`, `xmpp:` and relative paths; a transform
 * allowing only web URLs would empty every ref link.
 */
export function safeUrl(url: string): string {
  return /^(?:https?:\/\/|#\/)/i.test(url) ? url : "";
}
