import type { ReactNode } from "react";

/** A view's top bar: its icon and title, then its actions. */
export function ViewHeader({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="view-h">
      <h1 className="ttl">
        {icon}
        <span>{title}</span>
      </h1>
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
