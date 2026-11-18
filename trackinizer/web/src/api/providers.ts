import type { BalanceBody } from "./billing";
import { client, send, TIMEOUT_MS } from "./client";
import type { components } from "./generated/schema";
import type { Profile } from "./me";

/** The provider a station's chat talks to: a chat's own run, or the server's assistant. */
export type Provider = components["schemas"]["ChatProvider"];

/** Every provider a station's chat can talk to, the server's assistant first. */
export const PROVIDERS: readonly Provider[] = ["assistant", "claude", "codex"];

/** Whether `value` names a provider. */
export function isProvider(value: unknown): value is Provider {
  return typeof value === "string" && (PROVIDERS as readonly string[]).includes(value);
}

/**
 * What a station read says of its provider: the one it talks to, which are available, and why not. A station from a
 * server older than these fields has none: its provider is the assistant and every provider is available.
 */
export type StationProviders = Partial<Pick<components["schemas"]["WorkspaceState"], "provider" | "available" | "reasons">>;

/** The provider a station's chat talks to. */
export function stationProvider(station: StationProviders | null | undefined): Provider {
  return isProvider(station?.provider) ? station.provider : "assistant";
}

/** The server's assistant, by the actor it is configured as; null on a server with none. */
export type StationAssistant = { readonly assistant?: string | null };

/** What a provider is called on a person's screen: a CLI by its name, the assistant by the server's own. */
export function providerName(provider: Provider, station: StationAssistant | null | undefined): string {
  return provider === "assistant" ? station?.assistant || "assistant" : provider;
}

/** Whether the server has an assistant at all: a station read from a server with none says `assistant: null`. */
export function hasAssistant(station: StationAssistant | null | undefined): boolean {
  return station?.assistant !== null;
}

/** Why the assistant is out when the station says it is, and gives no reason: the caller's credit is spent. */
export const OUT_OF_CREDIT = "You are out of credit.";

/** Why the assistant is out: the server has none configured. */
export const NO_ASSISTANT = "No assistant on this server";

/** Why a CLI provider is out: no capable machine of the caller's. */
export function noMachineReason(provider: Provider): string {
  return `Connect a machine with \`trax machine connect\` and install ${provider} on it`;
}

/**
 * Whether one provider can take a chat now, and the reason it cannot (empty when it can). `credit` marks a reason that
 * is about the caller's credit, which Settings is where to answer.
 */
export type ProviderStatus = { readonly ok: boolean; readonly reason: string; readonly credit?: true };

/**
 * Each provider's status: the assistant is out when the server has none, or when the site's billing addon answers
 * that the caller may not run (`can_run`, with its `reason`); otherwise a provider is out when the station read says
 * so. A station read without the fields leaves every provider available.
 */
export function providerStatuses(station: (StationProviders & StationAssistant) | null | undefined, balance: BalanceBody | null): Record<Provider, ProviderStatus> {
  return Object.fromEntries(PROVIDERS.map((provider) => [provider, statusOf(provider, station, balance)])) as Record<Provider, ProviderStatus>;
}

function statusOf(provider: Provider, station: (StationProviders & StationAssistant) | null | undefined, balance: BalanceBody | null): ProviderStatus {
  if (provider === "assistant" && !hasAssistant(station)) return { ok: false, reason: NO_ASSISTANT };
  if (provider === "assistant" && balance !== null && !balance.can_run) return { ok: false, reason: balance.reason, credit: true };
  if (station?.available?.[provider] !== false) return { ok: true, reason: "" };
  const reason = station.reasons?.[provider];
  if (reason) return { ok: false, reason };
  return provider === "assistant" ? { ok: false, reason: OUT_OF_CREDIT, credit: true } : { ok: false, reason: noMachineReason(provider) };
}

/** The label a provider's chat shows: the server's (provider and machine), else the partner's name, else `fallback`. */
export function partnerLabel(partner: { readonly label?: string | null; readonly actor?: string | null } | null | undefined, fallback: string): string {
  return partner?.label || partner?.actor || fallback;
}

/** The default provider in the user's preferences, as the profile carries them; the assistant until one is chosen. */
export function profileChatProvider(profile: Profile): Provider {
  const chosen: unknown = profile.preferences?.chat_provider;
  return isProvider(chosen) ? chosen : "assistant";
}

/** Make `provider` the default for new chats: a PATCH of the user's preference, answered with every preference. */
export async function setChatProviderPreference(provider: Provider, { signal }: { signal?: AbortSignal } = {}): Promise<void> {
  await send(TIMEOUT_MS.write, signal, (signal) => client.PATCH("/api/me/preferences", {
    body: { chat_provider: provider }, signal,
  }));
}
