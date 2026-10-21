import { useMutation, useQuery } from "@tanstack/react-query";
import { balanceQuery, dollars, topUp } from "../api/billing";
import { ApiError } from "../api/client";
import { useOnline } from "../ui/bars";
import { KeyValue, ReadError, Section } from "./account";

/**
 * Settings > Billing: what the caller was granted, bought and spent, what is left, and the last day of their grace.
 * The figures and the verdict are the server's (`balanceQuery`); this page decides nothing. A user on their own
 * ChatGPT login has no balance to show (`--`) but still sees what they spent. Nothing shows where the site has no
 * billing addon. `assign` loads Stripe's checkout page, as `location.assign` does; tests pass a fake.
 */
export function BillingSection({ assign }: { assign: (url: string) => void }) {
  const read = useQuery(balanceQuery);
  const online = useOnline();
  const pay = useMutation({ mutationFn: topUp, onSuccess: (url) => assign(url) });
  const balance = read.data;
  if (!balance) return <ReadError read={read} />;
  const failure = pay.error instanceof ApiError && pay.error.detail ? pay.error.detail : "Could not start the payment.";
  return (
    <Section title="Billing">
      <ReadError read={read} />
      <KeyValue label="Granted">{dollars(balance.granted_usd)}</KeyValue>
      <KeyValue label="Purchased">{dollars(balance.purchased_usd)}</KeyValue>
      <KeyValue label="Spent">{dollars(balance.spent_usd)}</KeyValue>
      <KeyValue label="Left">{balance.own_login ? "--" : dollars(balance.usd)}</KeyValue>
      {balance.grace_until !== null && <KeyValue label="Grace">until {balance.grace_until}</KeyValue>}
      <p className="st-note">{balance.reason}</p>
      {balance.topup_usd.length > 0 && (
        <div className="st-row">
          {balance.topup_usd.map((usd) => (
            <button key={usd} type="button" className="btn" disabled={!online || pay.isPending} onClick={() => pay.mutate(usd)}>
              Add {dollars(usd).replace(/\.00$/, "")}
            </button>
          ))}
          {pay.isError && <span className="form-err" role="alert">{failure}</span>}
        </div>
      )}
    </Section>
  );
}
