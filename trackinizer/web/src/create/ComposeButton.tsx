import { useMeta, useWriteMode } from "../app/boot";
import { useRouter } from "../router/router";
import { Icon } from "../ui/icons";
import { kindLook } from "../ui/kinds";
import { createKindFor, creatableKinds } from "./draft";

/**
 * The compose button beside search in the sidebar: New, for the kind on screen.
 * Viewers do not see it. Apart from the create form, which loads as a chunk of
 * its own when first opened (src/router/views.ts).
 */
export function ComposeButton({ onNavigate }: { onNavigate: () => void }) {
  const { kinds } = useMeta();
  const { route, navigate } = useRouter();
  const mode = useWriteMode();
  const kind = createKindFor(route, creatableKinds(kinds));
  if (mode === "hidden" || !kind) return null;
  return (
    <button
      type="button"
      className="icon-btn compose-btn"
      disabled={mode === "disabled"}
      onClick={() => {
        onNavigate();
        navigate({ name: "new", kind });
      }}
      title={`New ${kindLook(kind).one} (C)`}
      aria-label={`New ${kindLook(kind).one}`}
    >
      <Icon name="compose" size={15} />
    </button>
  );
}
