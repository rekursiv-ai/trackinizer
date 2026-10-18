import { identityOf } from "./identity";

/** The identity this build was made with (`define` in vite.config.ts puts its key in). */
export const APP = identityOf(__APP__);
