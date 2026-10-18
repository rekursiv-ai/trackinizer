import { afterEach, expect, test, vi } from "vitest";
import { ApiError } from "../api/client";
import { answerCapture } from "../api/workspaces";
import { answerRequest, HIDDEN_WAIT_MS } from "./answer";
import { captureStage } from "./captureStage";
import { CaptureRegistry, type CaptureSource } from "./stageRegistry";

vi.mock("../api/workspaces", () => ({ answerCapture: vi.fn() }));
vi.mock("./captureStage", () => ({ captureStage: vi.fn() }));

const source: CaptureSource = { stage: document.createElement("div"), boxes: () => [] };
const picture = { error: "stands in for a picture" };

function registry(offered: CaptureSource | null) {
  const registered = new CaptureRegistry();
  registered.set(offered);
  return registered;
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

test("the open canvas is pictured and the picture answers the request under its id", async () => {
  vi.mocked(captureStage).mockResolvedValue(picture);

  await answerRequest("w1", "req", registry(source), { hidden: false });

  expect(captureStage).toHaveBeenCalledWith(source);
  expect(answerCapture).toHaveBeenCalledWith("w1", "req", picture);
});

test("with no canvas on screen the answer says so, and nothing is drawn", async () => {
  await answerRequest("w1", "req", registry(null), { hidden: false });

  expect(captureStage).not.toHaveBeenCalled();
  expect(answerCapture).toHaveBeenCalledWith("w1", "req", { error: expect.stringContaining("No canvas is on screen") });
});

test("a hidden tab holds back, so the tab the person is looking at answers first", async () => {
  vi.useFakeTimers();
  vi.mocked(captureStage).mockResolvedValue(picture);

  const pending = answerRequest("w1", "req", registry(source), { hidden: true });
  await vi.advanceTimersByTimeAsync(HIDDEN_WAIT_MS - 1);
  expect(answerCapture).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(1);
  await pending;

  expect(answerCapture).toHaveBeenCalledOnce();
});

test("an answer the server refuses, because another tab was first, is not an error", async () => {
  vi.mocked(captureStage).mockResolvedValue(picture);
  vi.mocked(answerCapture).mockRejectedValue(new ApiError(404, "No such capture request"));

  await expect(answerRequest("w1", "req", registry(source), { hidden: false })).resolves.toBeUndefined();
});
