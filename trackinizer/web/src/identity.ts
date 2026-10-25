// What the app calls itself. Everything a person sees of the name comes from it: the install manifest, the
// page title, the strings in the UI. Pure data, so the build (Node) and the page (browser) read the same one.
import { APP_BASE } from "./base";

/** One identity: the name and look an installed app has. */
export type AppIdentity = {
  readonly name: string;
  readonly short_name: string;
  readonly theme_color: string;
  readonly background_color: string;
  readonly icons: readonly { readonly src: string; readonly sizes: string; readonly type: string; readonly purpose: string }[];
};

export const TRACKINIZER: AppIdentity = {
  name: "Trackinizer",
  short_name: "Trackinizer",
  theme_color: "#090a0b",
  background_color: "#090a0b",
  icons: [
    { src: `${APP_BASE}icon-192.png`, sizes: "192x192", type: "image/png", purpose: "any maskable" },
    { src: `${APP_BASE}icon-512.png`, sizes: "512x512", type: "image/png", purpose: "any maskable" },
  ],
};

/** The web app manifest of an identity: installs as its own window over `/trax/`. */
export function manifestOf(identity: AppIdentity) {
  return {
    name: identity.name,
    short_name: identity.short_name,
    description: "Issues, beliefs, papers, experiments and agent sessions as one graph, updated live.",
    id: APP_BASE,
    start_url: APP_BASE,
    scope: APP_BASE,
    display: "standalone",
    theme_color: identity.theme_color,
    background_color: identity.background_color,
    icons: identity.icons,
  };
}

/** `html` with every `%APP_NAME%` placeholder replaced by the identity's name. */
export function fillName(html: string, identity: AppIdentity): string {
  return html.replaceAll("%APP_NAME%", identity.name);
}
