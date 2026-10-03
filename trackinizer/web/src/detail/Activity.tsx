import { useDeferredValue, useId } from "react";
import type { Change, Detail } from "../api/detail";
import { useMeta } from "../app/boot";
import { Markdown } from "../markdown/Markdown";
import { formatRoute } from "../router/route";
import { Avatar, RefChip } from "../ui/glyphs";
import { Icon } from "../ui/icons";
import { type ActivityItem, activityItems, describeChange, type Mention, type Phrase } from "./timeline";
import type { Field } from "./fields";
import { dateTime, relativeTime } from "./time";
import "./timeline.css";

/** The inquiry's last changes, oldest first, as `/api/web/get` sends them (at most 50). */
export function Activity({ detail, fields, now }: { detail: Detail; fields: readonly Field[]; now: number }) {
  const { edges: topology, kinds } = useMeta();
  const describe = (change: Change) => describeChange(change, { detail, topology, fields });
  const count = detail.changes.length;
  const id = useId();
  // Below the fold: it renders after the rest of the page, in a background
  // render React can interrupt. In the same task, a hub's detail took 51 to 62 ms
  // with the CPU slowed 4x, over the plan's 50 ms.
  const shown = useDeferredValue(true, false);
  if (!shown) return null;
  return (
    <section className="sec" aria-labelledby={id}>
      <div className="sec-h">
        <h2 id={id}>
          Activity <span className="count">{count >= LIMIT ? `last ${LIMIT}` : count}</span>
        </h2>
      </div>
      {count ? (
        <ol className="timeline">
          {activityItems(detail.changes).map((item) => (
            <Line key={keyOf(item)} item={item} describe={describe} kinds={kinds} now={now} />
          ))}
        </ol>
      ) : (
        <p className="unset">No changes yet.</p>
      )}
    </section>
  );
}

function Line({
  item,
  describe,
  kinds,
  now,
}: {
  item: ActivityItem;
  describe: (change: Change) => Phrase;
  kinds: readonly string[];
  now: number;
}) {
  const changes = item.type === "change" ? [item.change] : item.changes;
  const last = changes[changes.length - 1];
  const when = (
    <span className="tl-time" title={dateTime(last.created)}>
      · {relativeTime(last.created, now)}
    </span>
  );
  if (changes.length > 1) {
    const mentions = new Map(changes.map(describe).flatMap((phrase) => phrase.filter(isMention).map((m) => [m.id, m])));
    return (
      <li>
        <Avatar actor={last.actor} size={20} />
        <details className="tl-body">
          <summary>
            <b>{last.actor}</b> {changes.length} upstream changes {when}
          </summary>
          <p className="tl-mentions">
            {[...mentions.values()].map((mention) => (
              <MentionLink key={mention.id} mention={mention} />
            ))}
          </p>
        </details>
      </li>
    );
  }
  return (
    <li>
      <Avatar actor={last.actor} size={20} />
      <div className="tl-body">
        <b>{last.actor}</b> <PhraseText phrase={describe(last)} /> {when}
        {last.reason ? (
          <blockquote>
            <Markdown source={last.reason} kinds={kinds} />
          </blockquote>
        ) : null}
      </div>
    </li>
  );
}

function PhraseText({ phrase }: { phrase: Phrase }) {
  return phrase.map((part, index) =>
    typeof part === "string" ? part : <MentionLink key={`${index}:${part.id}`} mention={part} />,
  );
}

/** A named inquiry: its ref while it is still linked here, else a link by id. */
function MentionLink({ mention }: { mention: Mention }) {
  const { peer } = mention;
  if (peer) return <RefChip kind={peer.kind} seq={peer.seq} title={peer.title} />;
  return (
    <a className="ref" href={formatRoute({ name: "lookup", id: mention.id })} title={mention.id}>
      <Icon name="link" size={12} />
      {`${mention.kind} ${mention.id.slice(0, 8)}`}
    </a>
  );
}

function isMention(part: string | Mention): part is Mention {
  return typeof part !== "string";
}

function keyOf(item: ActivityItem): string {
  return item.type === "change" ? item.change.id : item.changes[0].id;
}

/** How many changes `/api/web/get` sends: `LIMIT 50` in `web_get`. */
const LIMIT = 50;
