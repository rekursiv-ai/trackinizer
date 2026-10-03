import { queryOptions } from "@tanstack/react-query";
import { inquiryKinds, listInquiries } from "../api/inquiries";
import { listTokens } from "../api/me";
import { ownerNames } from "./owners";

/**
 * Settings' reads. None is in the live stream: each refetches when Settings
 * opens and after the user's own writes.
 */
export const settingsQueries = {
  tokens: queryOptions({
    queryKey: ["settings", "tokens"],
    queryFn: ({ signal }) => listTokens({ signal }),
  }),
  /**
   * The owner names on rows under `email`'s account, most frequent first: one
   * list request over `kinds`, 200 rows per kind. It took 2.5 s on production,
   * so it runs only while Settings is open. The cache keeps the counts, not the
   * rows, so no write or live refetch elsewhere reruns it.
   */
  ownerNames: (email: string, kinds: readonly string[]) =>
    queryOptions({
      queryKey: ["settings", "owner-names", email],
      queryFn: async ({ signal }) =>
        ownerNames(
          await listInquiries(
            {
              kinds: inquiryKinds(kinds),
              filters: [
                { field: "account", op: "is", value: email },
                { field: "owner", op: "notnull", value: "" },
                { field: "owner", op: "ne", value: email },
              ],
              limit: 200,
              offset: 0,
              fields: ["owner"],
            },
            { signal },
          ),
        ),
    }),
};
