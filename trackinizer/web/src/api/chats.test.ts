import { expect, test } from "vitest";
import { isChatHandle } from "./chats";

test("a session whose routing name is chat- and twelve hex digits is a chat's", () => {
  expect(isChatHandle("chat-3d0e9f1a1b2c")).toBe(true);
  for (const other of ["chat-3d0e9f1a1b2", "chat-3d0e9f1a1b2cc", "chat-3D0E9F1A1B2C", "scout", "chat-xyz", "my-chat-3d0e9f1a1b2c"]) {
    expect(isChatHandle(other)).toBe(false);
  }
});
