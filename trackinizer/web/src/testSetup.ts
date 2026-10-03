// Runs before every test file (`setupFiles` in vite.config.ts); tests never import it.
import { focusManager, onlineManager } from "@tanstack/react-query";
import { cleanup } from "@testing-library/react";
import { configure, getConfig } from "@testing-library/dom";
import { beforeAll, vi } from "vitest";

const PRINT_LATER = Symbol.for("testSetup.printLater");

// Test files share their worker (`isolate: false`), so React, jsdom and Testing
// Library load and warm up once per worker, not once per file. What a fresh worker
// gave each file is put back here instead: the app's own modules, evaluated
// afresh, so their state (the client's failure counts, the debug log) and a
// file's `vi.mock`s are that file's alone.
vi.resetModules();

// The rest of a fresh worker: an empty document, empty web storage, the first
// URL, real timers, no stubbed globals, none of the browser APIs tests add, and
// TanStack Query's online and focus state, which it keeps once per worker.
beforeAll(() => {
  cleanup();
  onlineManager.setOnline(true);
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.unstubAllGlobals();
  for (const element of [document.documentElement, document.head, document.body]) {
    for (const { name } of [...element.attributes]) element.removeAttribute(name);
  }
  document.head.replaceChildren();
  document.body.replaceChildren();
  localStorage.clear();
  sessionStorage.clear();
  history.replaceState(null, "", "/");
  Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  Reflect.deleteProperty(navigator, "clipboard");
});

// A failed `getBy*` prints the whole document. Inside `waitFor` and `findBy*`,
// every check but the last is thrown away, and printing the document on each
// one cost up to 40 ms a test (560 ms in one). There, the printout is made only
// when the message is read: on a timeout, while the document is as it failed.
// Anywhere else it is made at once, before cleanup can empty the document.
// Testing Library's config outlives this file, so it is wrapped only once.
if (!Reflect.has(getConfig().getElementError, PRINT_LATER)) {
  const { getElementError } = getConfig();
  const printLater = (message: string | null, container: Element): Error => {
    if (Reflect.get(getConfig(), "_disableExpensiveErrorDiagnostics") !== true) return getElementError(message, container);
    const error = new Error();
    error.name = "TestingLibraryElementError";
    Object.defineProperty(error, "message", {
      configurable: true,
      get: () => {
        const { message: printed } = getElementError(message, container);
        Object.defineProperty(error, "message", { configurable: true, writable: true, value: printed });
        return printed;
      },
    });
    return error;
  };
  configure({ getElementError: Object.assign(printLater, { [PRINT_LATER]: true }) });
}
