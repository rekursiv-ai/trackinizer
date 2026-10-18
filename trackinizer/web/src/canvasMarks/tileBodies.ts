import { type RefCallback, useCallback, useRef, useState } from "react";

/**
 * The bodies of the stage's tiles by visual instance, which pinned boxes are
 * portaled into. A tile's ref callback puts its body here when it mounts, so a box
 * shows in the commit that adds its tile, with nothing measured or observed.
 */
export function useTileBodies() {
  const [bodies, setBodies] = useState<ReadonlyMap<string, HTMLElement>>(new Map());
  const refs = useRef(new Map<string, RefCallback<HTMLElement>>());
  // One callback per tile for its whole life: a new one each render would be called again each render.
  const bodyRef = useCallback((id: string): RefCallback<HTMLElement> => {
    let ref = refs.current.get(id);
    if (!ref) {
      ref = (element) => setBodies((now) => {
        if ((now.get(id) ?? null) === element) return now;
        const next = new Map(now);
        if (element) next.set(id, element);
        else next.delete(id);
        return next;
      });
      refs.current.set(id, ref);
    }
    return ref;
  }, []);
  return { bodies, bodyRef };
}

export type TileBodies = ReadonlyMap<string, HTMLElement>;
