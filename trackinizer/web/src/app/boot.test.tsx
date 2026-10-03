import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { Profile } from "../api/me";
import { ProfileContext, useWriteMode } from "./boot";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function Mode() {
  return <output>{useWriteMode()}</output>;
}

function profile(role: string): Profile {
  return { user_id: "u", email: "ada@example.com", name: "Ada", role, last_login: null, visual_workspace_enabled: false };
}

test("a viewer sees no write controls; others see them, disabled while offline", () => {
  const view = render(
    <ProfileContext value={profile("viewer")}>
      <Mode />
    </ProfileContext>,
  );
  expect(screen.getByRole("status").textContent).toBe("hidden");
  view.rerender(
    <ProfileContext value={profile("writer")}>
      <Mode />
    </ProfileContext>,
  );
  expect(screen.getByRole("status").textContent).toBe("enabled");

  const online = vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
  act(() => {
    dispatchEvent(new Event("offline"));
  });
  expect(screen.getByRole("status").textContent).toBe("disabled");
  online.mockReturnValue(true);
  act(() => {
    dispatchEvent(new Event("online"));
  });
  expect(screen.getByRole("status").textContent).toBe("enabled");
});
