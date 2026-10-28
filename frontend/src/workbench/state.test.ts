import { describe, it, expect } from "vitest";
import {
  applyEvent,
  applySnapshot,
  type WorkState,
  type RunRecord,
} from "./state";
const base: WorkState = {
  generation: 3,
  epoch: 7,
  topic_id: "B",
  runs: {},
  cursors: {},
};
const run: RunRecord = {
  run_id: "rA",
  topic_id: "A",
  scope: "BACKGROUND",
  epoch: 1,
  task_revision: 1,
  execution_version: 2,
  snapshot_seq: 12,
  state: "RUNNING",
};
describe("scoped results", () => {
  it("does not reduce current epoch on an immutable retry receipt or old HTTP response", () => {
    const updated = applySnapshot(base, run, 3);
    expect(updated.epoch).toBe(7);
    expect(updated.topic_id).toBe("B");
    expect(
      applySnapshot(updated, { ...run, state: "QUEUED", snapshot_seq: 5 }, 3)
        .runs.rA.state,
    ).toBe("RUNNING");
  });
  it("drops requests from an old account generation", () => {
    expect(applySnapshot(base, run, 2)).toEqual(base);
  });
  it("keeps SSE cursor separate and rejects old revision or execution output", () => {
    const current = applySnapshot(base, run, 3);
    const old = {
      seq: 20,
      run_id: "rA",
      topic_id: "A",
      scope: "BACKGROUND" as const,
      epoch: 1,
      task_revision: 1,
      execution_version: 1,
      kind: "run.succeeded",
      payload: { state: "SUCCEEDED" },
    };
    const next = applyEvent(current, old, 3);
    expect(next.runs.rA.state).toBe("RUNNING");
    expect(next.cursors.rA).toBe(20);
    expect(next.runs.rA.snapshot_seq).toBe(12);
  });
  it("allows background A to finish while B remains selected", () => {
    const current = applySnapshot(base, run, 3);
    const event = {
      seq: 13,
      run_id: "rA",
      topic_id: "A",
      scope: "BACKGROUND" as const,
      epoch: 1,
      task_revision: 1,
      execution_version: 2,
      kind: "run.succeeded",
      payload: { state: "SUCCEEDED" },
    };
    const next = applyEvent(current, event, 3);
    expect(next.topic_id).toBe("B");
    expect(next.runs.rA.state).toBe("SUCCEEDED");
  });
  it("does not apply delayed foreground A after A→B→A", () => {
    const current = {
      ...base,
      topic_id: "A",
      runs: { rA: { ...run, scope: "INTERACTIVE" as const } },
    };
    const next = applyEvent(
      current,
      {
        seq: 30,
        run_id: "rA",
        topic_id: "A",
        scope: "INTERACTIVE",
        epoch: 1,
        task_revision: 1,
        execution_version: 2,
        kind: "run.succeeded",
        payload: { state: "SUCCEEDED" },
      },
      3,
    );
    expect(next.runs.rA.state).toBe("RUNNING");
  });
});

it("ignores a stale running foreground HTTP response after returning to topic A", () => {
  const current = { ...base, topic_id: "A" };
  expect(applySnapshot(current, { ...run, scope: "INTERACTIVE" }, 3)).toEqual(
    current,
  );
});

it("model and query counters do not roll back on equal-sequence HTTP snapshots", () => {
  const current = {
    ...base,
    runs: { rA: { ...run, model_used: 2, query_used: 1, repair_used: 1 } },
  };
  const next = applySnapshot(
    current,
    { ...run, model_used: 1, query_used: 0, repair_used: 0 },
    3,
  );
  expect(next.runs.rA.model_used).toBe(2);
  expect(next.runs.rA.query_used).toBe(1);
  expect(next.runs.rA.repair_used).toBe(1);
});
