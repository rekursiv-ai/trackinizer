import type { RowsOnScreen } from "./list";

/** Which list rows are on screen, until `dispose`. */
export type RowWatch = RowsOnScreen & { dispose(): void };

/**
 * Watch the rows under `root`, the elements marked `data-row="<id>"` (as the
 * list's `Row` marks them): which are on screen, and `enter` with the ids of
 * rows that come on screen, by scrolling or by appearing, as when a group opens.
 *
 * Rows are observed as they are rendered, so only rows that exist are watched.
 * Without `IntersectionObserver` (jsdom), every rendered row counts as on screen.
 */
export function watchRows(root: HTMLElement, enter: (ids: string[]) => void): RowWatch {
  return typeof IntersectionObserver === "undefined" ? watchRendered(root, enter) : watchIntersecting(root, enter);
}

function watchIntersecting(root: HTMLElement, enter: (ids: string[]) => void): RowWatch {
  const visible = new Set<string>();
  const observed = new Map<HTMLElement, string>();
  // No `root` option: against the viewport, a row is still clipped by the
  // scrolling list around it, so a row scrolled out of the list is off screen.
  const intersection = new IntersectionObserver((entries) => {
    const entered: string[] = [];
    for (const entry of entries) {
      const id = observed.get(entry.target as HTMLElement);
      if (id === undefined) continue;
      if (!entry.isIntersecting) visible.delete(id);
      else if (!visible.has(id)) {
        visible.add(id);
        entered.push(id);
      }
    }
    if (entered.length > 0) enter(entered);
  });
  const sync = () => {
    for (const [element, id] of observed) {
      if (element.isConnected) continue;
      intersection.unobserve(element);
      observed.delete(element);
      visible.delete(id);
    }
    for (const element of root.querySelectorAll<HTMLElement>("[data-row]")) {
      if (observed.has(element) || !element.dataset.row) continue;
      observed.set(element, element.dataset.row);
      intersection.observe(element);
    }
  };
  const mutations = new MutationObserver(sync);
  mutations.observe(root, { childList: true, subtree: true });
  sync();
  return {
    onScreen: (ids) => new Set([...ids].filter((id) => visible.has(id))),
    dispose: () => {
      mutations.disconnect();
      intersection.disconnect();
    },
  };
}

function watchRendered(root: HTMLElement, enter: (ids: string[]) => void): RowWatch {
  let rendered = renderedRows(root);
  const mutations = new MutationObserver(() => {
    const now = renderedRows(root);
    const entered = [...now].filter((id) => !rendered.has(id));
    rendered = now;
    if (entered.length > 0) enter(entered);
  });
  mutations.observe(root, { childList: true, subtree: true });
  return {
    onScreen: (ids) => {
      const now = renderedRows(root);
      return new Set([...ids].filter((id) => now.has(id)));
    },
    dispose: () => mutations.disconnect(),
  };
}

function renderedRows(root: HTMLElement): Set<string> {
  return new Set([...root.querySelectorAll<HTMLElement>("[data-row]")].flatMap((row) => row.dataset.row ?? []));
}
