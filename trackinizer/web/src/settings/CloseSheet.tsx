import { SHEET, visited } from "../router/trail";
import { Icon } from "../ui/icons";

/** Close a sheet, Settings or Admin: back to the page under it, or to the graph when the tab opened on the sheet. */
export function CloseSheet({ label }: { readonly label: string }) {
  const back = visited().findLast((hash) => !SHEET.test(hash)) ?? "#/graph";
  return (
    <a className="icon-btn" href={back} aria-label={label} title={label}>
      <Icon name="x" />
    </a>
  );
}
