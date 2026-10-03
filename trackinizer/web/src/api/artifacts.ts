import { client, send, TIMEOUT_MS, type CallOptions } from "./client";
import type { components } from "./generated/schema";
import type { ArtifactContentRevision } from "../visuals/Artifact";

type WireRevision = components["schemas"]["ArtifactContentRevision"];

/** Fetch one exact, team-readable Artifact revision. */
export async function getArtifactContentRevision(
  id: string,
  { signal }: CallOptions = {},
): Promise<ArtifactContentRevision> {
  const value: WireRevision = await send(TIMEOUT_MS.read, signal, (signal) =>
    client.GET("/api/artifacts/{artifact_id}/content", {
      params: { path: { artifact_id: id } }, signal,
    }));
  if (!value || value.artifact_id.toLowerCase() !== id.toLowerCase()) {
    throw new Error("Invalid Artifact revision");
  }
  const common = {
    revision: value.revision,
    artifact_id: value.artifact_id,
    issue_id: value.issue_id,
    title: value.title,
    summary: value.summary,
    author: value.author,
    created_at: value.created_at,
    scope: value.scope ?? "team",
    citations: value.citations,
  };
  if (value.format === "html") {
    if (typeof value.html !== "string") throw new Error("Invalid HTML Artifact");
    return { ...common, format: "html", html: value.html };
  }
  if (value.format === "structured" && Array.isArray(value.sections)) {
    return { ...common, format: "structured", sections: value.sections };
  }
  throw new Error("Invalid Artifact format");
}
