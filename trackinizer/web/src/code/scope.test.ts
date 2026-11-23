import { expect, test } from "vitest";
import type { WorkspaceState } from "../api/workspaces";
import { codeScope, scopeName } from "./scope";

const SESSION = "11111111-1111-1111-1111-111111111111";
const OWN = "22222222-2222-2222-2222-222222222222";
const STREAM = "33333333-3333-3333-3333-333333333333";

function station(fields: Partial<WorkspaceState>): WorkspaceState {
  return { id: "s", name: "s", page: null, conversation_id: null, revision: 1, visuals: [], ...fields } as WorkspaceState;
}

test("the live run's session wins, since the station's own id can lag the conversation", () => {
  const scope = codeScope(station({ partner: { session_id: SESSION } as never, session_id: OWN, workstream_id: STREAM }));
  expect(scope).toEqual({ record: SESSION, kind: "session" });
});

test("the station's own session is used when no run is attached", () => {
  expect(codeScope(station({ session_id: OWN, workstream_id: STREAM }))).toEqual({ record: OWN, kind: "session" });
  // A partner with no session of its own does not shadow the station's.
  expect(codeScope(station({ partner: { session_id: null } as never, session_id: OWN }))).toEqual({
    record: OWN,
    kind: "session",
  });
});

test("with no session at all the workstream carries the code, so the pane works before the first message", () => {
  // Without this the feature is dead on a fresh station: there is nowhere to drop a folder
  // until someone has already started chatting, which is backwards.
  expect(codeScope(station({ workstream_id: STREAM }))).toEqual({ record: STREAM, kind: "workstream" });
});

test("a station naming neither has nowhere to file code, and says so rather than guessing one", () => {
  expect(codeScope(station({}))).toBeNull();
  expect(codeScope(undefined)).toBeNull();
});

test("the scope is named where a reader sees it, since the two do not live equally long", () => {
  expect(scopeName("session")).toBe("this session");
  expect(scopeName("workstream")).toBe("this workstream");
});
