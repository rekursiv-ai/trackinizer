import { createContext, type ReactNode, useContext, useState } from "react";
import type { Detail } from "../api/detail";
import { useMeta, useWriteMode } from "../app/boot";
import { useCommands } from "../commands/registry";
import { type CreatableKind, creatableKinds } from "../create/draft";
import type { IconName } from "../ui/icons";
import { LinkDialog } from "./LinkDialog";
import { SupersedeDialog } from "./SupersedeDialog";
import { type RelationChoice, relationChoices, SUPERSEDES } from "./topology";

/** One way to change an inquiry's relations: a title, its icon and key, if any, and what it opens. */
export type RelationAction = {
  readonly id: string;
  readonly title: string;
  readonly icon: IconName;
  readonly keys?: readonly string[];
  /** Open it; focus goes back to `returnTo` on close, or to what had focus before. */
  readonly run: (returnTo?: HTMLElement | null) => void;
};

/**
 * The detail's relation actions, for its ⋯ menu and its Add relation button:
 * add a relation, and supersede with an existing inquiry or a new one. Empty
 * for a viewer; offline, the controls that run them are off, and the palette
 * and the R key do not offer them.
 */
export function useRelationActions(): readonly RelationAction[] {
  const actions = useContext(ActionsContext);
  if (!actions) throw new Error("useRelationActions needs a RelationFlows above it.");
  return actions;
}

/**
 * Hold the relation dialogs of `detail`'s inquiry and offer their actions to
 * `children`, to the palette under the inquiry's `Kind#seq`, and to the R key.
 *
 * Supersede is offered when the topology lets an inquiry of this kind be
 * superseded: by an existing inquiry, one edge `POST`; by a new one, one batch,
 * for a kind a person creates.
 */
export function RelationFlows({ detail, children }: { detail: Detail; children: ReactNode }) {
  const { edges } = useMeta();
  const mode = useWriteMode();
  const [open, setOpen] = useState<Open | null>(null);
  const self = detail.self;
  const supersede = relationChoices(self.kind, edges).find(
    (choice) => choice.edgeKind === SUPERSEDES && choice.direction === "in",
  );
  const [creatable] = creatableKinds([self.kind]);
  const show = (flow: Flow) => (returnTo?: HTMLElement | null) =>
    setOpen({ flow, returnTo: returnTo ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null) });
  const actions: RelationAction[] =
    mode === "hidden"
      ? []
      : [
          { id: "relation.add", title: "Add relation…", icon: "plus", keys: ["r"], run: show({ type: "link", preset: null }) },
          ...(supersede
            ? [
                {
                  id: "relation.supersede",
                  title: "Supersede with an existing inquiry…",
                  icon: "swap",
                  run: show({ type: "link", preset: supersede }),
                } as const,
              ]
            : []),
          ...(supersede && creatable
            ? [
                {
                  id: "relation.supersede-new",
                  title: "Supersede with a new inquiry…",
                  icon: "swap",
                  run: show({ type: "new", kind: creatable }),
                } as const,
              ]
            : []),
        ];
  useCommands(
    mode === "enabled"
      ? actions.map(({ id, title, keys, run }) => ({ id, title, keys, section: `${self.kind}#${self.seq}`, run: () => run() }))
      : [],
  );
  const close = () => setOpen(null);
  return (
    <ActionsContext value={actions}>
      {children}
      {open?.flow.type === "link" ? (
        <LinkDialog detail={detail} preset={open.flow.preset} returnTo={open.returnTo} onClose={close} />
      ) : null}
      {open?.flow.type === "new" ? (
        <SupersedeDialog detail={detail} kind={open.flow.kind} returnTo={open.returnTo} onClose={close} />
      ) : null}
    </ActionsContext>
  );
}

/** Which dialog is open: the link picker, from its first step or at a preset relation, or a new superseding inquiry. */
type Flow = { readonly type: "link"; readonly preset: RelationChoice | null } | { readonly type: "new"; readonly kind: CreatableKind };

type Open = { readonly flow: Flow; readonly returnTo: HTMLElement | null };

const ActionsContext = createContext<readonly RelationAction[] | null>(null);
