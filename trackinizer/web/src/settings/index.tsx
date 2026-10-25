import { useQuery } from "@tanstack/react-query";
import { bootQueries, useProfile } from "../app/boot";
import { NO_AUTH_EMAIL, useSignOut } from "../app/signOut";
import { CopyDetails } from "../debug/CopyDetails";
import { dateTime, relativeTime, useMinuteClock } from "../detail/time";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { ViewHeader } from "../ui/view";
import { usePreferences } from "../preferences";
import { chooseSkin, chooseTheme, type Skin, SKINS, type ThemePreference, useTheme } from "../theme";
import { KeyValue, ReadError, Section } from "./account";
import { AliasesSection } from "./Aliases";
import { PreferencesDocument } from "./PreferencesDocument";
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
          <PreferencesDocument />
          <TokensSection />
          {profile.email === NO_AUTH_EMAIL ? null : <SessionSection assign={assign} />}
        </div>
      </div>
    </div>
  );
}

function ThemeSection() {
  const { choice: theme } = useTheme();
  const { skin } = usePreferences();
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
    </Section>
  );
}

/** Sign out (`useSignOut`), with its progress and failure in the row. */
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
