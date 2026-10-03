import { queryOptions } from "@tanstack/react-query";
import { listAllowlist, listUsers } from "../api/admin";

/**
 * Admin's reads. Neither is in the live stream: each refetches when Admin opens
 * and after the admin's own writes.
 */
export const adminQueries = {
  users: queryOptions({ queryKey: ["admin", "users"], queryFn: ({ signal }) => listUsers({ signal }) }),
  allowlist: queryOptions({ queryKey: ["admin", "allowlist"], queryFn: ({ signal }) => listAllowlist({ signal }) }),
};
