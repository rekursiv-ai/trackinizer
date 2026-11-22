// Where the app lives on its server, which serves the build there (server/web.py).
// Pure data, so the build (vite.config.ts) and the page read the same.

/** The app's own path: Vite builds for it, and installs and sign-in returns name it. */
export const APP_BASE = "/trax/";
