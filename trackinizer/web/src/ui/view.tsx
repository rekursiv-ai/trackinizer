import { createContext, type ReactNode, useContext } from "react";
import { Icon } from "./icons";

/** Opens the sidebar, which is a drawer on narrow screens. */
export const OpenDrawerContext = createContext<() => void>(() => {});

/**
 * A view's top bar: its icon and title, then its actions.
 *
 * On narrow screens it starts with the button that opens the sidebar.
 */
export function ViewHeader({
  icon,
  title,
  beside,
  children,
}: {
  icon: ReactNode;
  title: string;
  /** Sits next to the title, before the spacer; for a control that belongs to the view's name. */
  beside?: ReactNode;
  children?: ReactNode;
}) {
  const openDrawer = useContext(OpenDrawerContext);
  return (
    <header className="view-h">
      <button
        type="button"
        className="icon-btn only-mobile"
        onClick={openDrawer}
        aria-label="Open navigation"
      >
        <Icon name="menu" />
      </button>
      <h1 className="ttl">
        {icon}
        <span>{title}</span>
      </h1>
      {beside}
      <div className="spacer" />
      {children}
    </header>
  );
}

/** A centred note in an empty view. */
export function EmptyState({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon}
      <h3>{title}</h3>
      {children}
    </div>
  );
}
