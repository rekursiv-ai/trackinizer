import { ForceSim2D } from "./layout";

export type WorkerIn =
  | {
      type: "set";
      gen: number;
      count: number;
      x: Float32Array;
      y: Float32Array;
      vx: Float32Array;
      vy: Float32Array;
      linkFrom: Int32Array;
      linkTo: Int32Array;
      linkStrength: Float32Array;
      groupId: Int32Array;
      reheat: boolean;
    }
  | {
      type: "group";
      gen: number;
      groupId: Int32Array;
      linkStrength: Float32Array;
      reheat: boolean;
    }
  | { type: "stop" };

export type WorkerOut = {
  type: "positions";
  gen: number;
  x: Float32Array;
  y: Float32Array;
  settled: boolean;
};

const sim = new ForceSim2D();
let currentGen = 0;
let timer: ReturnType<typeof setTimeout> | null = null;

function clearTimer(): void {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function sendPositions(settled: boolean): void {
  const xCopy = new Float32Array(sim.x);
  const yCopy = new Float32Array(sim.y);
  const msg: WorkerOut = {
    type: "positions",
    gen: currentGen,
    x: xCopy,
    y: yCopy,
    settled,
  };
  self.postMessage(msg, { transfer: [xCopy.buffer, yCopy.buffer] });
}

function stepLoop(): void {
  timer = null;
  // Run up to 16 ms of physics ticks per batch so 1.7k-20k graphs converge rapidly
  // without flooding postMessage.
  const t0 = performance.now();
  let running = true;
  let ticks = 0;
  while (running && ticks < 4) {
    running = sim.tick();
    ticks += 1;
    if (performance.now() - t0 >= 12) break;
  }
  sendPositions(!running);
  if (running) {
    timer = setTimeout(stepLoop, 0);
  }
}

self.onmessage = (event: MessageEvent<WorkerIn>) => {
  const msg = event.data;
  if (msg.type === "set") {
    clearTimer();
    currentGen = msg.gen;
    sim.setGraph(msg, msg.reheat);
    if (sim.alpha > 0) stepLoop();
    else sendPositions(true);
  } else if (msg.type === "group") {
    clearTimer();
    currentGen = msg.gen;
    sim.setGroups(msg.groupId, msg.linkStrength, msg.reheat);
    if (sim.alpha > 0) stepLoop();
    else sendPositions(true);
  } else if (msg.type === "stop") {
    clearTimer();
  }
};
