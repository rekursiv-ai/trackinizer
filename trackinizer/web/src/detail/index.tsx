import { useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, type ReactNode, startTransition, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { markPageDrawn } from "../debug/timings";
import { ApiError } from "../api/client";
import type { Detail, DetailRow } from "../api/detail";
import { useMeta, useWriteMode } from "../app/boot";
import { useIsHighlighted } from "../app/highlights";
import { PurgeDialog } from "../bulk/Purge";
import { useCommands } from "../commands/registry";
import { CopyDetails } from "../debug/CopyDetails";
import { TextEditor, TitleEditor } from "../editors/TextEditor";
import { Menu } from "../lists/Menu";
import { useLiveDetail } from "../live";
import { Markdown, useMarkdownWarm } from "../markdown/Markdown";
import { ProgressiveMarkdown } from "../markdown/ProgressiveMarkdown";
import { cachedInquiries } from "../palette/sources";
import { RelationFlows, useRelationActions } from "../relations/flows";
import { formatRoute, type Route } from "../router/route";
import { useRouter } from "../router/router";
import { Bar } from "../ui/bars";
import { capitalize } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { KindIcon, kindLook } from "../ui/kinds";
import { type PanelSpec, PanelToggle, panelCommand, usePanel } from "../ui/panel";
import { EmptyState, OpenDrawerContext } from "../ui/view";
import { Activity } from "./Activity";
import { type Field, kindFields, usd } from "./fields";
import { GraphPreview } from "./GraphPreview";
import { Metrics } from "./metrics";
import { Properties } from "./Properties";
import { detailQueries, markOpened } from "./queries";
import { Rail } from "./Rail";
import { Relations } from "./Relations";
import { dateTime, relativeTime, useMinuteClock } from "./time";
import { Transcript } from "./transcript";
import "../writes/writes.css";
import "./detail.css";

const ArtifactContent = lazy(() => import("../visuals/Artifact").then((module) => ({ default: module.ArtifactPage })));

/** Which inquiry a detail shows: by kind and seq, or by id. */
export type DetailTarget = { kind: string; seq: number } | { id: string };

/**
 * One inquiry, Details lens: `#/ref/<kind>/<seq>` or `#/lookup/<id>`.
 *
 * A `Kind#seq` link resolves to its id first, since `/api/web/get` takes only
 * an id: from a row the app already holds when it can (a list row, a detail or
 * one of its relations' far ends), else by `GET /api/inquiries/{Kind}/{seq}`.
 * Keyed by target, so moving to another inquiry starts fresh.
 */
export function DetailView({ target }: { target: DetailTarget }) {
  if ("id" in target) return <DetailById key={target.id} id={target.id} kind={null} name={target.id} />;
  return <DetailByRef key={`${target.kind}#${target.seq}`} kind={target.kind} seq={target.seq} />;
}

function DetailByRef({ kind, seq }: { kind: string; seq: number }) {
  const queryClient = useQueryClient();
  const ref = useQuery({
    ...detailQueries.ref(kind, seq),
    initialData: () => cachedInquiries(queryClient).find((held) => held.kind === kind && held.seq === seq)?.id,
  });
  if (ref.data) return <DetailById id={ref.data} kind={kind} name={`${kind}#${seq}`} />;
  return (
    <Frame kind={kind} name={`${kind}#${seq}`} busy={!ref.isError}>
      {ref.isError ? <Failure error={ref.error} name={`${kind}#${seq}`} retry={() => void ref.refetch()} /> : <Loading />}
    </Frame>
  );
}

/**
 * The inquiry `id`; until it loads, the frame shows `kind` and `name`, what the
 * link named.
 *
 * Its page draws once Markdown is warm (`useMarkdownWarm`), and when it or the
 * inquiry came after the view did, in a background render React can interrupt
 * (https://react.dev/reference/react/startTransition), where the parts it
 * defers still come after. Drawn at once, a hub's page, its first Markdown with
 * it, was one task of 61 to 64 ms with the CPU slowed 4x on a Xeon.
 */
function DetailById({ id, kind, name }: { id: string; kind: string | null; name: string }) {
  const query = useQuery(detailQueries.detail(id));
  const queryClient = useQueryClient();
  useEffect(() => markOpened(queryClient, id), [queryClient, id]);
  useLiveDetail(id);
  const ready = useMarkdownWarm() && query.data !== undefined;
  const [shown, setShown] = useState(ready);
  useEffect(() => {
    if (ready && !shown) startTransition(() => setShown(true));
  }, [ready, shown]);
  // A row purged while open answers 404 on refetch: say so rather than show it stale.
  const gone = query.error instanceof ApiError && query.error.status === 404;
  const drawn = !!query.data && !gone && shown;
  // After an agent's navigation here, the page drawn with its data is what its timing mark waits for.
  useLayoutEffect(() => {
    if (drawn) markPageDrawn(window.location.hash);
  }, [drawn, id]);
  if (query.data && !gone && shown) {
    return (
      <Page
        detail={query.data}
        stale={query.isRefetchError ? query.error : null}
        retry={() => void query.refetch()}
      />
    );
  }
  return (
    <Frame kind={kind} name={name} busy={!query.isError}>
      {query.isError ? <Failure error={query.error} name={name} retry={() => void query.refetch()} /> : <Loading />}
    </Frame>
  );
}

/**
 * The loaded inquiry: its header and text (the description and the other
 * Markdown fields), then the rail (its parents, children and properties), then
 * the rest: config, content, metrics, transcript, other relations and activity.
 * Wide, the rail stands beside them all; narrow, it comes after the text, in
 * this reading order. Its button in the top bar, or `]`, collapses it, for the
 * tab.
 */
function Page({ detail, stale, retry }: { detail: Detail; stale: Error | null; retry: () => void }) {
  const { fieldOwners, kinds } = useMeta();
  const now = useMinuteClock();
  const row = detail.self;
  const fields = useMemo(() => kindFields(row, fieldOwners), [row, fieldOwners]);
  const at = (place: Field["look"]["place"]) => fields.filter((field) => field.look.place === place);
  const cost = fields.reduce(
    (sum, field) => sum + (field.look.format === "usd" && typeof field.value === "number" ? field.value : 0),
    0,
  );
  const rail = usePanel(RAIL);
  const railId = useId();
  useCommands([panelCommand(rail)]);
  return (
    <RelationFlows detail={detail}>
      <Frame
        kind={row.kind}
        name={`${row.kind}#${row.seq}`}
        actions={
          <>
            <ShowInGraph row={row} />
            <MoreMenu detail={detail} />
            <PanelToggle panel={rail} controls={railId} />
          </>
        }
      >
        {stale ? (
          <Bar kind="stale">
            Could not refresh {detail.self.kind}#{detail.self.seq}: {stale.message}
            <button type="button" className="btn ghost" onClick={retry}>
              Retry
            </button>
            <CopyDetails message={`Could not refresh ${detail.self.kind}#${detail.self.seq}: ${stale.message}`} error={stale} />
          </Bar>
        ) : null}
        <div className="d-scroll">
          <div className="d-grid">
            <div className="d-top">
              <div className="d-main-inner">
                <Head detail={detail} now={now} cost={cost} />
                {at("text").map((field) => (
                  <TextEditor key={field.name} detail={detail} field={field}>
                    <TextField field={field} kinds={kinds} />
                  </TextEditor>
                ))}
              </div>
            </div>
            <div className="d-rail" id={railId} hidden={rail.collapsed}>
              <GraphPreview detail={detail} />
              <Rail detail={detail} />
              <Properties detail={detail} fields={fields} />
            </div>
            <div className="d-main">
              <div className="d-main-inner">
                {at("json").map((field) => (
                  <TextEditor key={field.name} detail={detail} field={field} json>
                    <JsonField field={field} />
                  </TextEditor>
                ))}
                {row.kind === "Artifact" && <Suspense fallback={<p className="d-loading">Loading Artifact…</p>}>
                  <ArtifactContent id={row.id} optional />
                </Suspense>}
                <Metrics row={row} />
                <Transcript row={row} />
                <Relations detail={detail} />
                <Activity detail={detail} fields={fields} now={now} />
              </div>
            </div>
          </div>
        </div>
      </Frame>
    </RelationFlows>
  );
}

/**
 * Show in graph: the graph focused on this inquiry, lit two hops out. A link,
 * so a ⌘-click opens it in a tab, and a palette command under the inquiry's
 * `Kind#seq`. Under the graph's route the detail is the graph's Peek, already
 * where it would lead, so it offers neither.
 */
function ShowInGraph({ row }: { row: DetailRow }) {
  const { route, navigate } = useRouter();
  const inGraph = route.name === "graph";
  const graph: Route = { name: "graph", focus: { ref: { kind: row.kind, seq: row.seq }, hops: 2 } };
  useCommands(
    inGraph ? [] : [{ id: "detail.graph", title: "Show in graph", section: `${row.kind}#${row.seq}`, run: () => navigate(graph) }],
  );
  if (inGraph) return null;
  return (
    <a className="btn ghost" href={formatRoute(graph)} title={`Show ${row.kind}#${row.seq} in the graph`}>
      <Icon name="graph" size={14} />
      <span className="hide-sm">Show in graph</span>
    </a>
  );
}

/**
 * The inquiry's ⋯ menu: the actions on it that are not a field, such as adding
 * a relation, superseding it, or purging it, last, as the mock has it. A viewer
 * has none, so it does not show.
 */
function MoreMenu({ detail }: { detail: Detail }) {
  const actions = useRelationActions();
  const offline = useWriteMode() === "disabled";
  const trigger = useRef<HTMLButtonElement>(null);
  const [purging, setPurging] = useState(false);
  if (!actions.length) return null;
  const name = `${detail.self.kind}#${detail.self.seq}`;
  return (
    <>
      <Menu
        label={`${name} actions…`}
        trigger={
          <button ref={trigger} type="button" className="icon-btn" aria-label={`${name} actions`} title="More actions" disabled={offline}>
            <Icon name="more" />
          </button>
        }
        options={[
          ...actions.map(({ id, title, icon, keys }) => ({
            value: id,
            label: title,
            icon: <Icon name={icon} size={14} />,
            hint: keys?.[0]?.toUpperCase(),
          })),
          { value: PURGE, label: "Purge…", icon: <Icon name="trash" size={14} />, danger: true },
        ]}
        onPick={(id) => (id === PURGE ? setPurging(true) : actions.find((action) => action.id === id)?.run(trigger.current))}
      />
      {purging ? <PurgeDialog detail={detail} returnTo={trigger.current} onClose={() => setPurging(false)} /> : null}
    </>
  );
}

/** The ⋯ menu's value for Purge; no relation action has this id. */
const PURGE = "inquiry.purge";

/** The rail as a panel: on the right, and `]` collapses or expands it. */
const RAIL: PanelSpec = { id: "detail.rail", name: "parents, children and properties", side: "right", keys: ["]"] };

/**
 * The header: the inquiry's kind, its own cost, when it changed, and its title,
 * which edits in place. Its parents are the rail's.
 *
 * The cost is this inquiry's two axes summed, named as its own, not its
 * subtree's. The server stores an unrecorded cost as 0, so 0
 * reads "none recorded", never "$0.00".
 */
function Head({ detail, now, cost }: { detail: Detail; now: number; cost: number }) {
  const row = detail.self;
  const one = kindLook(row.kind).one;
  const highlighted = useIsHighlighted(row.id);
  return (
    <div className={highlighted ? "d-head is-highlighted" : "d-head"}>
      <div className="eyebrow">
        <KindIcon kind={row.kind} size={13} />
        {capitalize(one)}
        <span className="d-cost">
          Cost of this {one} <b>{cost ? usd(cost) : "none recorded"}</b>
        </span>
        <span className="d-dates">
          Created <b title={dateTime(row.created)}>{dateTime(row.created)}</b> · Updated{" "}
          <b title={dateTime(row.modified)}>{relativeTime(row.modified, now)}</b>
        </span>
      </div>
      <TitleEditor detail={detail} />
    </div>
  );
}

/**
 * The description as the page's body, a part at a time when long; other
 * Markdown fields as labelled callouts. `TextEditor` around it opens its editor.
 */
function TextField({ field, kinds }: { field: Field; kinds: readonly string[] }) {
  const text = typeof field.value === "string" ? field.value : undefined;
  if (field.name === "description") {
    return (
      <div data-field="description">
        {text ? <ProgressiveMarkdown source={text} kinds={kinds} /> : <p className="md-empty">No description.</p>}
      </div>
    );
  }
  return (
    <section className="callout" aria-label={field.look.label} data-field={field.name}>
      <span className="k">{field.look.label}</span>
      {text ? <Markdown source={text} kinds={kinds} /> : <span className="unset">Not set</span>}
    </section>
  );
}

/** A JSON object field, such as an Experiment's config, folded until opened. */
function JsonField({ field }: { field: Field }) {
  return (
    <details className="cfg" data-field={field.name}>
      <summary>
        <Icon name="chevR" size={13} />
        {field.look.label}
        {field.value === undefined ? <span className="unset"> — not set</span> : null}
      </summary>
      {field.value === undefined ? null : <pre>{JSON.stringify(field.value, null, 2)}</pre>}
    </details>
  );
}

/**
 * The view's frame: the top bar with the way back to the kind's list and any
 * `actions`, then `children`. Esc goes back too. While loading it shows what it
 * was asked for.
 */
function Frame({
  kind,
  name,
  busy = false,
  actions,
  children,
}: {
  kind: string | null;
  name: string;
  busy?: boolean;
  /** At the top bar's right: the loaded inquiry's Show in graph and ⋯ menu. */
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { navigate } = useRouter();
  const openDrawer = useContext(OpenDrawerContext);
  useCommands(
    kind
      ? [{ id: "detail.back", title: `Back to ${kindLook(kind).plural}`, keys: ["Escape"], run: () => navigate({ name: "list", kind }) }]
      : [],
  );
  return (
    <div className="view detail" aria-busy={busy}>
      <div className="detail-top">
        <button type="button" className="icon-btn only-mobile" onClick={openDrawer} aria-label="Open navigation">
          <Icon name="menu" />
        </button>
        <nav className="crumbs" aria-label="Breadcrumb">
          {kind ? (
            <>
              <a className="crumb-btn" href={formatRoute({ name: "list", kind })} title="Back (Esc)">
                <Icon name="arrowL" size={14} />
                <span className="hide-sm">{kindLook(kind).plural}</span>
              </a>
              <span className="crumb-sep hide-sm">/</span>
            </>
          ) : null}
          <span className="crumb-cur" aria-current="page">
            {kind ? <KindIcon kind={kind} size={14} /> : <Icon name="link" size={14} />}
            <span className="mono">{name}</span>
          </span>
        </nav>
        <div className="spacer" />
        {actions}
      </div>
      {children}
    </div>
  );
}

function Loading() {
  return <p className="d-loading">Loading…</p>;
}

/** Why the inquiry could not be shown: gone (404), or the server's message with Retry. */
function Failure({ error, name, retry }: { error: Error; name: string; retry: () => void }) {
  if (error instanceof ApiError && error.status === 404) {
    return (
      <EmptyState icon={<Icon name="trash" size={24} />} title="Deleted or purged">
        <p>
          <code className="mono">{name}</code> is not in Trackinizer.
        </p>
      </EmptyState>
    );
  }
  return (
    <EmptyState icon={<Icon name="x" size={24} />} title="Could not load this inquiry">
      <p role="alert">{error.message}</p>
      <span className="w-actions">
        <button type="button" className="btn" onClick={retry}>
          Retry
        </button>
        <CopyDetails message={`Could not load ${name}: ${error.message}`} error={error} />
      </span>
    </EmptyState>
  );
}
