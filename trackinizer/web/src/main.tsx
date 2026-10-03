import { StrictMode } from "react";
import { prefetch } from "./app/prefetch";
import { restoreReturnHash } from "./app/session";
import { installDebug, logRenderError } from "./debug/install";
import { openEarlyStream } from "./live/earlyStream";
import { chunk, reloadOnChunkError } from "./router/lazy";
import { installTheme } from "./theme";

installDebug();
installTheme();
restoreReturnHash();
reloadOnChunkError();
// Before the first reads, so that it is open by the time the views mount and
// read: their reads then miss no change, and none has to read again.
openEarlyStream();
prefetch(location.hash);
// React DOM and the app are chunks of their own, which this small entry fetches
// together; each is evaluated in a task of its own as it arrives. As one module
// graph they took one task of 71 to 101 ms with the CPU slowed 4x, over the
// plan's 50 ms for any task.
Promise.all([chunk(import("react-dom/client")), chunk(import("./start"))])
  .then(async ([{ createRoot }, { App, preloadView }]) => {
    // Awaited: a view whose chunk is still loading suspends its first render,
    // and React then shows the fallback for at least 300 ms however soon the
    // chunk arrives. Not awaited, a detail link was slower than with no
    // prefetch at all.
    await preloadView(location.hash);
    // A render error goes to the log, once, in place of React's own console line;
    // the app's crash screen (in App) shows it.
    createRoot(document.getElementById("root")!, { onCaughtError: logRenderError, onUncaughtError: logRenderError }).render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  })
  .catch((error: unknown) => {
    // Without React there is no crash screen: say so rather than leave the page
    // blank. Thrown on, the error is logged as unhandled (installDebug).
    document.getElementById("root")!.textContent = `Trackinizer could not load: ${String(error)}. Reload to try again.`;
    throw error;
  });
