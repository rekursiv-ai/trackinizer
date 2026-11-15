import type { ReactNode } from "react";

/** A view's top bar: its icon and title, then its actions. */
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
  return (
    <header className="view-h">
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
