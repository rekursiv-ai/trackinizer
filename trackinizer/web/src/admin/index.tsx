import { useQuery } from "@tanstack/react-query";
import { bootQueries, useProfile } from "../app/boot";
import { ReadError } from "../settings/account";
import { Icon } from "../ui/icons";
import { EmptyState, ViewHeader } from "../ui/view";
import { AllowlistSection } from "./Allowlist";
import { UsersSection } from "./Users";
import "../editors/editors.css";
import "../writes/writes.css";
import "../settings/settings.css";

/**
 * Admin, `#/admin`: users (role, disable and enable, delete) and the allowlist
 * (add, change role, remove). Anyone else who follows a link here is told it
 * is for admins, and nothing is requested: the server would refuse it (403).
 * The role is read again as the page opens, and admin reads wait for it, so an
 * admin demoted since boot requests nothing either.
 */
export function AdminView() {
  const refetched = useQuery(bootQueries.profile);
  const admin = useProfile().role === "admin";
  return (
    <div className="view">
      <ViewHeader icon={<Icon name="shield" />} title="Admin" />
      <div className="scroll">
        {admin ? (
          <div className="st">
            <ReadError read={refetched} />
            {refetched.isFetchedAfterMount ? (
              <>
                <UsersSection />
                <AllowlistSection />
              </>
            ) : (
              <p className="st-note">Checking your role…</p>
            )}
          </div>
        ) : (
          <EmptyState icon={<Icon name="shield" size={24} />} title="Admins only">
            <p>Users and the allowlist are managed by admins. Ask an admin for access.</p>
            <ReadError read={refetched} />
          </EmptyState>
        )}
      </div>
    </div>
  );
}
