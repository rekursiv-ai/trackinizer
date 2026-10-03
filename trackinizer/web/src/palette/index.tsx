import { useQueryClient } from "@tanstack/react-query";
import { Dialog } from "radix-ui";
import { type KeyboardEvent, type RefObject, useEffect, useId, useRef, useState } from "react";
import { useMeta } from "../app/boot";
import { usePalette } from "../commands/palette";
import { keyCaps, useCommandList } from "../commands/registry";
import { CopyDetails } from "../debug/CopyDetails";
import { useRouter } from "../router/router";
import { Avatar, StateGlyphs } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { type Option, paletteOptions, SERVER_SECTION } from "./options";
import "./palette.css";
import { type KindSearch, useLoaded, useServerSearch } from "./sources";

/**
 * The ⌘K palette: a Radix Dialog around a combobox over a listbox, in the
 * WAI-ARIA pattern. Focus stays in the input; the arrows move the active row
 * (`aria-activedescendant`) and Enter picks it. Commands and inquiries the app
 * already holds show while typing, the server's results after a pause.
 */
export function PaletteView() {
  const palette = usePalette();
  const opener = useRef<HTMLElement | null>(null);
  const input = useRef<HTMLInputElement>(null);
  return (
    <Dialog.Root open={palette.open} onOpenChange={(open) => (open ? palette.show() : palette.hide())}>
      <Dialog.Portal>
        <Dialog.Overlay className="pal-backdrop" />
        <Dialog.Content
          className="palette"
          aria-describedby={undefined}
          // Radix returns focus only to a Dialog.Trigger, and the palette has
          // none: ⌘K, the sidebar and search links all open it. So it keeps the
          // element that had focus and returns focus there itself.
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
            input.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (opener.current?.isConnected) opener.current.focus();
            opener.current = null;
          }}
        >
          <Dialog.Title className="sr-only">Command menu</Dialog.Title>
          <Combobox key={palette.query} initialText={palette.query} inputRef={input} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** The input, its rows and the footer; mounted afresh each time the palette opens. */
function Combobox({ initialText, inputRef }: { initialText: string; inputRef: RefObject<HTMLInputElement | null> }) {
  const palette = usePalette();
  const { navigate } = useRouter();
  const { kinds } = useMeta();
  const queryClient = useQueryClient();
  const commands = useCommandList();
  const [text, setText] = useState(initialText);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  // A search link names what it wants, so it searches at once.
  const { searches, waiting, searchNow } = useServerSearch(text, kinds, { immediate: initialText !== "" });
  const { cached, recent } = useLoaded(queryClient);
  const options = paletteOptions({ text, kinds, cached, recent, commands, searches });
  // Tracked by key, not position, so rows arriving from the server never move it.
  const found = options.findIndex((option) => option.key === activeKey);
  const activeIndex = found < 0 && options.length ? 0 : found;
  const idPrefix = useId();
  const optionId = (index: number) => `${idPrefix}-option-${index}`;
  const listId = `${idPrefix}-list`;

  useEffect(() => {
    document.getElementById(`${idPrefix}-option-${activeIndex}`)?.scrollIntoView?.({ block: "nearest" });
  }, [idPrefix, activeIndex]);

  const pick = (option: Option) => {
    switch (option.type) {
      case "inquiry":
        palette.hide();
        navigate(option.route);
        return;
      case "command":
        palette.hide();
        option.command.run();
        return;
      case "retry":
        option.retry();
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!options.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActiveKey(options[(activeIndex + step + options.length) % options.length]!.key);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (activeIndex < 0) searchNow();
      else pick(options[activeIndex]!);
    }
  };

  const taken = snapshotTime(searches);
  const status = serverStatus(searches, waiting, options, taken);
  const failure = searches.find((search) => search.error)?.error ?? null;
  return (
    <>
      <div className="pal-input">
        <Icon name="search" size={18} />
        <input
          ref={inputRef}
          role="combobox"
          aria-label="Command"
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeIndex < 0 ? undefined : optionId(activeIndex)}
          autoComplete="off"
          spellCheck={false}
          placeholder="Search, jump to Issue#412 or a UUID; title:re and description:re work too…"
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setActiveKey(null);
          }}
          onKeyDown={onKeyDown}
        />
      </div>
      <div className="pal-list" id={listId} role="listbox" aria-label="Results">
        {sectionsOf(options).map(({ section, rows }, n) => (
          <div key={`${n}-${section}`} role="group" aria-labelledby={`${idPrefix}-section-${n}`}>
            <div className="pal-sec" id={`${idPrefix}-section-${n}`}>
              {section === SERVER_SECTION && taken ? `${section} · as of ${taken}` : section}
            </div>
            {rows.map(({ option, index }) => (
              <div
                key={option.key}
                id={optionId(index)}
                role="option"
                aria-selected={index === activeIndex}
                className={index === activeIndex ? "pal-item is-active" : "pal-item"}
                // Keep focus in the input, where the arrows and Enter work.
                onMouseDown={(event) => event.preventDefault()}
                onPointerMove={() => option.key !== activeKey && setActiveKey(option.key)}
                onClick={() => pick(option)}
              >
                <OptionBody option={option} />
              </div>
            ))}
          </div>
        ))}
      </div>
      {!options.length && text.trim() && !status.pending && (
        <div className="pal-empty">No results for “{text.trim()}”</div>
      )}
      <p className="pal-status" role="status">
        {status.text}
      </p>
      {failure ? (
        <p className="pal-status">
          <CopyDetails message={`Search failed: ${failure.message}`} error={failure} />
        </p>
      ) : null}
      <div className="pal-foot">
        <span>
          <kbd>↑</kbd>
          <kbd>↓</kbd> navigate
        </span>
        <span>
          <kbd>↵</kbd> select
        </span>
        <span>
          <kbd>esc</kbd> close
        </span>
      </div>
    </>
  );
}

function OptionBody({ option }: { option: Option }) {
  switch (option.type) {
    case "inquiry": {
      const { found, route } = option;
      if (!found) {
        return (
          <>
            {route.name === "ref" ? <KindIcon kind={route.kind} size={15} /> : <Icon name="corner" size={15} />}
            <span className="ref-t">{route.name === "ref" ? `${route.kind}#${route.seq}` : route.id}</span>
            <span className="lbl muted">Open</span>
          </>
        );
      }
      return (
        <>
          <KindIcon kind={found.kind} size={15} />
          <span className="ref-t">{`${found.kind}#${found.seq}`}</span>
          <StateGlyphs status={found.status} judgement={found.judgement} />
          <span className="lbl">{found.title}</span>
          {found.owner && <Avatar actor={found.owner} size={16} />}
        </>
      );
    }
    case "command":
      return (
        <>
          <Icon name="chevR" size={15} />
          <span className="lbl">{option.command.title}</span>
          {option.command.keys?.[0] && (
            <span className="keys">
              {keyCaps(option.command.keys[0]).map((cap, index) => (
                <kbd key={index}>{cap}</kbd>
              ))}
            </span>
          )}
        </>
      );
    case "retry":
      return (
        <>
          <Icon name="x" size={15} />
          <span className="lbl pal-error">
            {option.kinds.map((kind) => kindLook(kind).plural).join(", ")}: {option.message}
          </span>
          <span className="keys">Retry</span>
        </>
      );
  }
}

/** Consecutive rows of one section, with each row's place in the whole list. */
function sectionsOf(options: readonly Option[]): { section: string; rows: { option: Option; index: number }[] }[] {
  const sections: { section: string; rows: { option: Option; index: number }[] }[] = [];
  options.forEach((option, index) => {
    const last = sections.at(-1);
    if (last?.section === option.section) last.rows.push({ option, index });
    else sections.push({ section: option.section, rows: [{ option, index }] });
  });
  return sections;
}

/**
 * What the server search is doing, for the status line: due once typing pauses
 * or under way, which both read as searching, or done with every result already
 * listed as loaded. Its results carry their time in their heading; when none are
 * left to show, this line does.
 */
function serverStatus(
  searches: readonly KindSearch[],
  waiting: boolean,
  options: readonly Option[],
  taken: string,
): { pending: boolean; text: string } {
  if (waiting || searches.some((search) => search.pending)) return { pending: true, text: "Searching the server…" };
  const added = options.some((option) => option.section === SERVER_SECTION);
  const nothing = searches.length > 0 && options.length > 0 && !added;
  return { pending: false, text: nothing ? `The server's results, as of ${taken}, are all listed above.` : "" };
}

/** When the server's answers were taken: the earliest, as a clock time. */
function snapshotTime(searches: readonly KindSearch[]): string {
  const times = searches.map((search) => search.updatedAt).filter((at) => at > 0);
  return times.length ? new Date(Math.min(...times)).toLocaleTimeString() : "";
}
