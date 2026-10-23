import { createContext, useContext } from "react";
import type { CanvasOperation } from "../api/canvases";

/** What a canvas lets the views inside it do to it. */
export type CanvasActions = {
  readonly busy: boolean;
  readonly writeError: string | null;
  /** Apply one canvas operation. Links never need it: they only set the address. */
  readonly operate: (operation: CanvasOperation) => void;
  /** The visual types this deployment's catalog offers. */
  readonly visualTypes: ReadonlySet<string>;
};

const CanvasActionsContext = createContext<CanvasActions | null>(null);

/** Provide the canvas operations that record views can invoke. */
export const CanvasActionsProvider = CanvasActionsContext;

/** Get the canvas operations shared by every visual in the canvas. */
export function useCanvasActions() {
  return useContext(CanvasActionsContext);
}
