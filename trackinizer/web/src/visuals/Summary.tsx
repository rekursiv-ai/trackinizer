import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { loadCampaignSummary, readTrust } from "../api/summaries";
import { useVisualMark } from "./marks";
import type { RendererProps } from "./registry";
import { SummaryCorpus } from "./SummaryCorpus";
import { SummarySession } from "./SummarySession";
import { SummarySessions } from "./SummarySessions";
import { Pill } from "./SummaryPill";
import { rowOfDoc } from "./summaryDoc";
import "./Summary.css";

type Tab = "sessions" | "session" | "corpus";

/** A running campaign's summary is rewritten about every ten minutes; a minute is soon enough to see it. */
const REFRESH_MS = 60_000;

/**
 * `trax.summary`: what a campaign did, for a reader who may not read its transcripts. Sessions lists
 * them as a tree, Session shows one with its steps, phases and beliefs, and Corpus groups the efforts
 * and claims they share. A reader the server lets read the transcript also gets it beside the steps,
 * and the item numbers open it; for any other reader those are numbers only.
 */
export function Summary({ instance, workspace }: RendererProps) {
  const recordId = instance.record_id;
  const trust = useQuery({ queryKey: ["me", "trust"], queryFn: ({ signal }) => readTrust({ signal }), staleTime: 300_000, retry: false });
  const accounts = trust.data?.accounts;
  const query = useQuery({
    queryKey: ["visual", "summary", recordId, accounts],
    queryFn: ({ signal }) => loadCampaignSummary(recordId!, accounts!, { signal }),
    enabled: !!recordId && !!accounts,
    retry: false,
    refetchInterval: (q) => (q.state.data?.corpus?.state === "closed" ? false : REFRESH_MS),
  });
  useVisualMark(instance, workspace, "data", query.isSuccess);
  const [tab, setTab] = useState<Tab>("sessions");
  const [selected, setSelected] = useState<string | null>(null);
  if (!recordId) return <div className="visual-unsupported">Choose a campaign or one of its sessions to read its summary.</div>;
  if (trust.isError) return <div className="visual-unsupported" role="alert">Could not load the summary.</div>;
  if (query.isPending) return <div className="visual-loading" aria-busy="true">Loading the summary…</div>;
  if (query.isError) {
    return <div className="visual-unsupported" role="alert">
      Could not load the summary.
      <button className="btn ghost" type="button" onClick={() => void query.refetch()}>Retry</button>
    </div>;
  }
  const summary = query.data;
  if (!summary) return <div className="visual-unsupported" role="status">No summary has been written for this yet. One appears a few minutes into a campaign.</div>;
  const staff = trust.data?.audiences.includes("staff") ?? false;
  const { corpus, sessions } = summary;
  const rows = corpus?.sessions ?? [...sessions.values()].map(rowOfDoc);
  const open = (session: string) => {
    setSelected(session);
    setTab("session");
  };
  const doc = selected ? sessions.get(selected) : undefined;
  const state = corpus?.state ?? ([...sessions.values()].every((d) => d.state === "closed") ? "closed" : "running");
  const tabs: readonly { readonly id: Tab; readonly label: string; readonly off?: boolean }[] = [
    { id: "sessions", label: "Sessions" },
    { id: "session", label: "Session", off: !doc },
    { id: "corpus", label: "Corpus", off: !corpus },
  ];
  return <section className="sum" aria-label="Campaign summary">
    <header className="sum-head">
      <div role="tablist" aria-label="Summary views">
        {tabs.map(({ id, label, off }) => <button key={id} type="button" role="tab" id={`sum-tab-${id}`}
          aria-selected={tab === id} aria-controls="sum-panel" disabled={off} onClick={() => setTab(id)}>{label}</button>)}
      </div>
      <span className="sum-state">{corpus?.campaign}{" "}
        <Pill value={state} />{corpus ? ` as of ${corpus.as_of.slice(0, 16).replace("T", " ")}` : ""}</span>
    </header>
    <div id="sum-panel" role="tabpanel" aria-labelledby={`sum-tab-${tab}`} className="sum-panel">
      {tab === "sessions" && <SummarySessions rows={rows} docs={sessions} onOpen={open} />}
      {tab === "session" && doc && <SummarySession key={doc.session} doc={doc} staff={staff} />}
      {tab === "corpus" && corpus && <SummaryCorpus corpus={corpus} docs={sessions} onOpen={open} />}
    </div>
  </section>;
}
