import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { useProfile } from "../app/boot";
import { accountKind, forgetAccount, type RememberedAccount, rememberAccount, signInUrl, useAccounts } from "../app/accounts";
import { SessionContext } from "../app/session";
import { NO_AUTH_EMAIL, useSignOut } from "../app/signOut";
import { sendWaiting, usePreferences } from "../preferences";
import { formatRoute } from "../router/route";
import { chooseSkin, chooseTheme, SKINS, type ThemePreference, useTheme } from "../theme";
import { useOnline } from "../ui/bars";
import { Icon } from "../ui/icons";
import { useToast } from "../ui/toast";
import { useDismiss } from "./useDismiss";

/** The theme choices the Default skin offers, as Settings names them. */
const THEMES: readonly { readonly theme: ThemePreference; readonly label: string }[] = [
  { theme: "light", label: "Light" },
  { theme: "dark", label: "Dark" },
  { theme: "system", label: "System" },
];

/**
 * The signed-in account at the top bar's end, as Google's is: the avatar
 * alone, which the pointer resting on it widens to show the email and role. A
 * click opens the menu: the account, Your settings, Admin (for admins),
 * Appearance (the skins, and with the Default skin the theme), the other
 * accounts this browser has signed in as (each opens Google's sign-in with it
 * picked, × forgets it here), Add another account, and Sign out last. A server
 * run with `--no-auth` has no one to sign in as: the accounts and Sign out are
 * left out there.
 */
export function AccountMenu({ onNavigate }: { readonly onNavigate: () => void }) {
  const profile = useProfile();
  const signedIn = profile.email !== NO_AUTH_EMAIL;
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(open, close, root, button);
  const accounts = useAccounts();
  const others = accounts.filter((each) => each.email !== profile.email);
  const { skin } = usePreferences();
  const { choice } = useTheme();
  const session = useContext(SessionContext);
  const online = useOnline();
  const toast = useToast();
  const { pending, leave } = useSignOut({ failed: ({ message, error }) => toast(`Could not sign out. ${message}`, { failed: true, error }) });

  // Remember this account in this browser as signed in now, its role as the server gives it.
  useEffect(() => {
    if (signedIn) rememberAccount({ email: profile.email, name: profile.name, role: profile.role });
  }, [signedIn, profile.email, profile.name, profile.role]);

  async function signInAs(email?: string) {
    // A draft still waiting to be sent goes first: the page is about to be left.
    await sendWaiting({ keepalive: true });
    const url = signInUrl(email);
    if (session) session.assign(url);
    else location.assign(url);
  }

  const role = <span className={`role-chip${profile.role === "admin" ? " is-admin" : ""}`}>{profile.role}</span>;
  return (
    <div className="topbar-pop-root account" ref={root}>
      <button ref={button} type="button" className="account-button" aria-haspopup="true" aria-expanded={open}
        aria-label={`Account: ${profile.email}, ${profile.role}`} onClick={() => setOpen(!open)}>
        <Avatar email={profile.email} />
        <span className="account-reveal" aria-hidden="true">
          <span className="account-email">{profile.email}</span>
          {role}
        </span>
      </button>
      {open && (
        <div className="topbar-pop account-menu" role="dialog" aria-label="Account">
          <div className="account-head">
            <Avatar email={profile.email} large />
            <strong className="account-head-email">{profile.email}</strong>
            <span className="account-head-meta">{role}<span>{orgLabel(profile.email)}</span></span>
          </div>
          <a className="topbar-item" href={formatRoute({ name: "settings" })} onClick={() => { close(); onNavigate(); }}>
            <Icon name="gear" /><span className="label">Your settings</span>
          </a>
          {profile.role === "admin" && (
            <a className="topbar-item" href={formatRoute({ name: "admin" })} onClick={() => { close(); onNavigate(); }}>
              <Icon name="shield" /><span className="label">Admin</span>
            </a>
          )}
          <section className="account-section" aria-label="Appearance">
            <p className="topbar-pop-label">Appearance</p>
            <div className="skin-grid" role="group" aria-label="Skin">
              {SKINS.map((each) => (
                <button key={each.skin} type="button" className="skin-option" aria-pressed={each.skin === skin}
                  onClick={() => chooseSkin(each.skin)}>{each.label}</button>
              ))}
            </div>
            {skin === "default" && (
              <div className="theme-choice" role="group" aria-label="Theme">
                {THEMES.map((each) => (
                  <button key={each.theme} type="button" className="skin-option" aria-pressed={each.theme === choice}
                    onClick={() => chooseTheme(each.theme)}>{each.label}</button>
                ))}
              </div>
            )}
          </section>
          {signedIn && (
            <section className="account-section" aria-label="Other accounts">
              <p className="topbar-pop-label">Other accounts on this browser</p>
              {others.map((each) => <OtherAccount key={each.email} account={each} onSwitch={() => void signInAs(each.email)} />)}
              <button type="button" className="topbar-item" onClick={() => void signInAs()}>
                <Icon name="user" /><span className="label">Add another account</span>
              </button>
            </section>
          )}
          {signedIn && (
            <div className="account-section">
              <button type="button" className="topbar-item account-sign-out" disabled={!online} aria-disabled={pending || undefined}
                onClick={() => void leave()}>
                <Icon name="logOut" /><span className="label">Sign out</span>
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Another account this browser remembers: a press signs in as it; × forgets it here. */
function OtherAccount({ account, onSwitch }: { readonly account: RememberedAccount; readonly onSwitch: () => void }) {
  const { kind } = accountKind(account.email);
  return (
    <div className="account-other">
      <button type="button" className="topbar-item" onClick={onSwitch} title={`Sign in as ${account.email}`}>
        <Avatar email={account.email} />
        <span className="label">
          <span className="account-other-email">{account.email}</span>
          <span className="account-other-meta">{account.role} · {orgLabel(account.email)}</span>
        </span>
        <span className="account-kind">{kind === "personal" ? "Personal" : "Work"}</span>
      </button>
      <button type="button" className="icon-btn account-forget" aria-label={`Forget ${account.email} on this browser`}
        title="Forget on this browser" onClick={() => forgetAccount(account.email)}><Icon name="x" size={12} /></button>
    </div>
  );
}

/** An account's first letter on a colour its email picks, the same in every list. */
function Avatar({ email, large = false }: { readonly email: string; readonly large?: boolean }) {
  let hash = 0;
  for (const char of email) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return (
    <span className={`avatar${large ? " avatar-large" : ""}`} aria-hidden="true"
      style={{ background: `var(--group-${(hash % 4) + 1})`, color: "var(--on-group)" }}>
      {email.charAt(0).toUpperCase()}
    </span>
  );
}

/** Personal for a mail host anyone joins; else the organisation's domain. */
function orgLabel(email: string): string {
  const { kind, org } = accountKind(email);
  return kind === "personal" ? "Personal" : org;
}
