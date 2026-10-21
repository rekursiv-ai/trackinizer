import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { setVisualWorkspacePreference } from "../api/me";
import { bootQueries, useProfile } from "../app/boot";
import { NO_AUTH_EMAIL, useSignOut } from "../app/signOut";
import { CopyDetails } from "../debug/CopyDetails";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { ViewHeader } from "../ui/view";
import { choosePreferences, usePreferences } from "../preferences";
import { chooseSkin, chooseTheme, type Skin, SKINS, type ThemePreference, useTheme } from "../theme";
import { KeyValue, ReadError, Section } from "./account";
import { AliasesSection } from "./Aliases";
import { BillingSection } from "./Billing";
import { CloseSheet } from "./CloseSheet";
import { ChatPartnerSection } from "./ChatPartner";
import { PreferencesDocument } from "./PreferencesDocument";
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
      <ViewHeader icon={<Icon name="gear" />} title="Your settings">
        <CloseSheet label="Close settings" />
      </ViewHeader>
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
          <PreferencesDocument />
          <VisualWorkspaceSection />
          <ChatPartnerSection />
          <BillingSection assign={assign} />
          <TokensSection />
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
  const toolCalls = usePreferences().chat_tool_calls;
  return (
    <Section title="Agent workspace">
      <div className="st-row">
        <label className="st-workspace-choice">
          <input type="checkbox" checked={enabled} disabled={!online || change.isPending}
            onChange={(event) => change.mutate(event.target.checked)} />
          Enable agent-guided workbench
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
      {/* The user's preference: Chat draws each tool call as a card between lines. */}
      <div className="st-row">
        <label className="st-workspace-choice">
          <input type="checkbox" checked={toolCalls} onChange={(event) => {
            void choosePreferences({ chat_tool_calls: event.target.checked });
          }} />
          Show tool calls in Chat
        </label>
        <span className="muted">Each call an agent makes shows as a card that opens to what it ran and what came back.</span>
      </div>
    </Section>
  );
}

/** The sizes the picker offers; a size the user set in the preferences document shows as well. */
const TERMINAL_FONT_SIZES: readonly string[] = ["11px", "12px", "13px", "14px", "16px", "18px"];

function ThemeSection() {
  const { choice: theme } = useTheme();
  const { skin, terminal_font_size: terminalFontSize } = usePreferences();
  const tabChords = usePreferences().terminal_tab_chords;
  // A fixed skin sets every colour itself (skins.css), so the theme has nothing to colour under it.
  const fixed = skin !== "default";
  return (
    <Section title="Appearance">
      <div className="st-row">
        <label htmlFor="appearance-skin">Skin</label>
        <select id="appearance-skin" className="st-theme" value={skin} onChange={(event) => {
          chooseSkin(event.target.value as Skin);
        }}>
          {SKINS.map((each) => <option key={each.skin} value={each.skin}>{each.label}</option>)}
        </select>
      </div>
      <div className="st-row">
        <label htmlFor="appearance-theme">Theme</label>
        <select id="appearance-theme" className="st-theme" value={theme} disabled={fixed}
          aria-describedby={fixed ? "appearance-theme-note" : undefined} onChange={(event) => {
            chooseTheme(event.target.value as ThemePreference);
          }}>
          <option value="dark">Dark</option>
          <option value="light">Light</option>
          <option value="system">System</option>
        </select>
        {fixed && <span id="appearance-theme-note" className="muted">The theme colours the Default skin only.</span>}
      </div>
      {/* One of the user's preferences, like the theme: the text size of the terminal Chat flips to. */}
      <div className="st-row">
        <label htmlFor="appearance-terminal-font">Terminal font size</label>
        <select id="appearance-terminal-font" className="st-theme" value={terminalFontSize} onChange={(event) => {
          void choosePreferences({ terminal_font_size: event.target.value });
        }}>
          {TERMINAL_FONT_SIZES.includes(terminalFontSize) ? null : <option value={terminalFontSize}>{terminalFontSize}</option>}
          {TERMINAL_FONT_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
        </select>
      </div>
      <div className="st-row">
        <label className="st-workspace-choice">
          <input type="checkbox" checked={tabChords} onChange={(event) => {
            void choosePreferences({ terminal_tab_chords: event.target.checked });
          }} />
          Ctrl+T, Ctrl+W and Ctrl+Tab open, close and switch terminal tabs
        </label>
        <span className="muted">Ctrl+Shift+T, Ctrl+Shift+W and Ctrl+Shift+PageUp/PageDown always do. In a plain browser tab Chrome keeps these chords; in the installed app the page gets them.</span>
      </div>
    </Section>
  );
}

/** Sign out, here or from the account menu (`useSignOut`), with its progress and failure in the row. */
function SessionSection({ assign }: { assign: (url: string) => void }) {
  const online = useOnline();
  const { pending, failure, leave } = useSignOut({ assign });
  return (
    <Section title="Session">
      <div className="st-row">
        <button type="button" className="btn ghost" disabled={!online} aria-disabled={pending || undefined} onClick={() => void leave()}>
          Sign out
        </button>
        {pending ? (
          <span className="w-status" role="status">
            Signing out…
          </span>
        ) : null}
        {failure ? (
          <span className="form-err" role="alert">
            Could not sign out. {failure.message}
            <CopyDetails message={`Could not sign out. ${failure.message}`} error={failure.error} />
          </span>
        ) : null}
      </div>
    </Section>
  );
}
