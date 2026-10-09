import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { setVisualWorkspacePreference, signOut } from "../api/me";
import { bootQueries, useProfile } from "../app/boot";
import { LOGIN_URL } from "../app/session";
import { CopyDetails } from "../debug/CopyDetails";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { ViewHeader } from "../ui/view";
import { chooseTheme, type ThemePreference, useTheme } from "../theme";
import { KeyValue, ReadError, Section } from "./account";
import { AliasesSection } from "./Aliases";
import { BrowserDataSection } from "./BrowserData";
import { ChatPartnerSection } from "./ChatPartner";
import { useHighlightMentions } from "./highlightMentions";
import { TokensSection } from "./Tokens";
import "../editors/editors.css";
import "../writes/writes.css";
import "./settings.css";

/**
 * Your settings, `#/settings`: the profile, the names that mean you, API
 * tokens, this browser's state, and sign out, except under `--no-auth`. Settings
 * are not in the live stream; each part refetches when the page opens and after
 * your own writes.
 *
 * `assign` loads another page, as `location.assign` does; tests pass a fake.
 */
export function SettingsView({ assign = (url: string) => location.assign(url) }: { assign?: (url: string) => void }) {
  // Refetched on open: the role may have changed since boot.
  const refetched = useQuery(bootQueries.profile);
  const profile = useProfile();
  const now = useMinuteClock();
  return (
    <div className="view">
      <ViewHeader icon={<Icon name="gear" />} title="Your settings" />
      <div className="scroll">
        <div className="st">
          <Section title="Profile">
            <ReadError read={refetched} />
            <KeyValue label="Name">{profile.name}</KeyValue>
            <KeyValue label="Email">{profile.email}</KeyValue>
            <KeyValue label="Role">{profile.role}</KeyValue>
            <KeyValue label="Last sign-in">
              {profile.last_login ? <span title={dateTime(profile.last_login)}>{relativeTime(profile.last_login, now)}</span> : "never"}
            </KeyValue>
          </Section>
          <AliasesSection />
          <ThemeSection />
          <VisualWorkspaceSection />
          <ChatPartnerSection />
          <TokensSection />
          <BrowserDataSection />
          {profile.email === NO_AUTH_EMAIL ? null : <SessionSection assign={assign} />}
        </div>
      </div>
    </div>
  );
}

function VisualWorkspaceSection() {
  const profile = useProfile();
  const queryClient = useQueryClient();
  const online = useOnline();
  const change = useMutation({
    mutationFn: setVisualWorkspacePreference,
    onSuccess: ({ enabled }) => {
      queryClient.setQueryData(bootQueries.profile.queryKey, { ...profile, visual_workspace_enabled: enabled });
    },
  });
  const enabled = change.data?.enabled ?? profile.visual_workspace_enabled;
  const [mentions, chooseMentions] = useHighlightMentions();
  return (
    <Section title="Agent workspace">
      <div className="st-row">
        <label className="st-workspace-choice">
          <input type="checkbox" checked={enabled} disabled={!online || change.isPending}
            onChange={(event) => change.mutate(event.target.checked)} />
          Enable agent-guided canvas
        </label>
        {change.isPending && <span className="w-status" role="status">Saving…</span>}
        {change.isError && <span className="form-err" role="alert">Could not save workspace preference.</span>}
      </div>
      {/* This browser's, like the theme: it changes how the page draws an answer, not what the server holds. */}
      <div className="st-row">
        <label className="st-workspace-choice">
          <input type="checkbox" checked={mentions} onChange={(event) => chooseMentions(event.target.checked)} />
          Highlight rows the assistant mentions
        </label>
      </div>
    </Section>
  );
}

function ThemeSection() {
  const { choice: theme } = useTheme();
  return (
    <Section title="Appearance">
      <div className="st-row">
        <label htmlFor="appearance-theme">Theme</label>
        <select id="appearance-theme" className="st-theme" value={theme} onChange={(event) => {
          chooseTheme(event.target.value as ThemePreference);
        }}>
          <option value="dark">Dark</option>
          <option value="light">Light</option>
          <option value="system">System</option>
        </select>
      </div>
    </Section>
  );
}

/**
 * Sign out: the server clears the session cookie, then the login page loads.
 *
 * Not a write through the write layer: it changes no data, a second request
 * does no harm, and its failures are about signing out, not saving.
 */
function SessionSection({ assign }: { assign: (url: string) => void }) {
  const online = useOnline();
  const [state, setState] = useState<{ readonly pending: boolean; readonly failure: { readonly message: string; readonly error: unknown } | null }>(IDLE);
  const leave = async () => {
    if (state.pending) return;
    setState({ pending: true, failure: null });
    try {
      await signOut();
    } catch (error) {
      setState({ pending: false, failure: { message: error instanceof Error ? error.message : String(error), error } });
      return;
    }
    assign(LOGIN_URL);
  };
  return (
    <Section title="Session">
      <div className="st-row">
        <button type="button" className="btn ghost" disabled={!online} aria-disabled={state.pending || undefined} onClick={() => void leave()}>
          Sign out
        </button>
        {state.pending ? (
          <span className="w-status" role="status">
            Signing out…
          </span>
        ) : null}
        {state.failure ? (
          <span className="form-err" role="alert">
            Could not sign out. {state.failure.message}
            <CopyDetails message={`Could not sign out. ${state.failure.message}`} error={state.failure.error} />
          </span>
        ) : null}
      </div>
    </Section>
  );
}

const IDLE = { pending: false, failure: null };

/**
 * The account a server run with `--no-auth` answers every request as
 * (`server/auth.py`). Nobody signs in there, so signing out would only land on
 * a login page with nothing to sign in with.
 */
const NO_AUTH_EMAIL = "no-auth@localhost";
