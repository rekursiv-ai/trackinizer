import { expect, test } from "vitest";
import { serverText } from "./pages";

test("a row's time reads as the server's filter text: UTC, a space, six digits of fraction or none", () => {
  expect(serverText("2026-09-27T07:06:59.930000+00:00")).toBe("2026-09-27 07:06:59.930000+00:00");
  expect(serverText("2026-09-27T07:06:59+00:00")).toBe("2026-09-27 07:06:59+00:00");
  expect(serverText("2026-09-27T09:06:59.000001+02:00")).toBe("2026-09-27 07:06:59.000001+00:00");
  expect(serverText("2026-09-27T07:06:59.5Z")).toBe("2026-09-27 07:06:59.500000+00:00");
});
