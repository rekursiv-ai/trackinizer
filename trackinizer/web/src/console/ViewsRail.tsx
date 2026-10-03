import { useState } from "react";
import { Icon } from "../ui/icons";
import type { View } from "./views";

/**
 * The rail's views: each opens on a click, and is renamed, pinned or deleted
 * in place; "New view" starts an Untitled one, which is listed once it changes.
 */
export function ViewsSection({
  views,
  open,
  onOpen,
  onEdit,
  onRemove,
}: {
  views: readonly View[];
  open: View;
  /** Open view `id`, or a new one. */
  onOpen: (id: string | null) => void;
  onEdit: (id: string, change: (view: View) => View) => void;
  onRemove: (id: string) => void;
}) {
  return (
    <section className="console-sec" aria-label="Views">
      <h2 className="console-h">Views</h2>
      <ul className="console-views">
        {views.map((view) => (
          <ViewRow key={view.id} view={view} current={view.id === open.id} onOpen={onOpen} onEdit={onEdit} onRemove={onRemove} />
        ))}
      </ul>
      <button type="button" className="console-new" onClick={() => onOpen(null)}>
        <Icon name="plus" size={13} />
        New view
      </button>
    </section>
  );
}

function ViewRow({
  view,
  current,
  onOpen,
  onEdit,
  onRemove,
}: {
  view: View;
  current: boolean;
  onOpen: (id: string) => void;
  onEdit: (id: string, change: (view: View) => View) => void;
  onRemove: (id: string) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const rename = (name: string) => {
    setRenaming(false);
    if (name.trim()) onEdit(view.id, (one) => ({ ...one, name: name.trim() }));
  };
  return (
    <li className="console-view">
      {renaming ? (
        <input
          className="console-view-name"
          aria-label="View name"
          defaultValue={view.name}
          // The rename starts from the row's own Rename button, so the box takes the focus it left.
          autoFocus
          onBlur={(event) => rename(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") rename(event.currentTarget.value);
            if (event.key === "Escape") setRenaming(false);
          }}
        />
      ) : (
        <button type="button" className="console-view-open" aria-current={current ? "page" : undefined} onClick={() => onOpen(view.id)}>
          {view.name}
        </button>
      )}
      <button type="button" className="icon-btn" aria-label={`Rename ${view.name}`} onClick={() => setRenaming(true)}>
        <Icon name="edit" size={13} />
      </button>
      <button
        type="button"
        className="icon-btn console-pin"
        aria-label={`${view.pinned ? "Unpin" : "Pin"} ${view.name}`}
        aria-pressed={view.pinned}
        onClick={() => onEdit(view.id, (one) => ({ ...one, pinned: !one.pinned }))}
      >
        <Icon name={view.pinned ? "starFill" : "star"} size={13} />
      </button>
      <button type="button" className="icon-btn" aria-label={`Delete ${view.name}`} onClick={() => onRemove(view.id)}>
        <Icon name="trash" size={13} />
      </button>
    </li>
  );
}
