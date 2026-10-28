import type { Run, RunEvent } from "../api/types";
export type RunRecord = Pick<
  Run,
  | "run_id"
  | "topic_id"
  | "scope"
  | "epoch"
  | "task_revision"
  | "execution_version"
  | "snapshot_seq"
  | "state"
> &
  Partial<Run> & { stage?: string; event_seq?: number };
export type EventRecord = Omit<RunEvent, "created_at"> &
  Partial<Pick<RunEvent, "created_at">>;
export type WorkState = {
  generation: number;
  epoch: number;
  topic_id: string;
  runs: Record<string, RunRecord>;
  cursors: Record<string, number>;
};
const terminal = new Set([
  "SUCCEEDED",
  "FAILED",
  "TIMED_OUT",
  "CANCELLED",
  "SUPERSEDED",
]);
const states = new Set(["QUEUED", "RUNNING", "WAITING_INPUT", ...terminal]);
export function applySnapshot(
  state: WorkState,
  run: RunRecord,
  generation: number,
): WorkState {
  if (generation !== state.generation) return state;
  if (
    run.scope === "INTERACTIVE" &&
    !terminal.has(run.state) &&
    (run.epoch !== state.epoch || run.topic_id !== state.topic_id)
  )
    return state;
  const current = state.runs[run.run_id];
  if (current) {
    if (run.task_revision < current.task_revision) return state;
    if (
      run.task_revision === current.task_revision &&
      run.execution_version < current.execution_version
    )
      return state;
    if (
      run.snapshot_seq < Math.max(current.snapshot_seq, current.event_seq ?? 0)
    )
      return state;
    if (
      run.snapshot_seq === current.snapshot_seq &&
      terminal.has(current.state) &&
      !terminal.has(run.state)
    )
      return state;
  }
  return {
    ...state,
    runs: {
      ...state.runs,
      [run.run_id]: {
        ...run,
        stage: current?.stage,
        event_seq: current?.event_seq,
        model_used: Math.max(run.model_used ?? 0, current?.model_used ?? 0),
        query_used: Math.max(run.query_used ?? 0, current?.query_used ?? 0),
        repair_used: Math.max(run.repair_used ?? 0, current?.repair_used ?? 0),
        attempts: Math.max(run.attempts ?? 0, current?.attempts ?? 0),
      },
    },
  };
}
export function applyEvent(
  state: WorkState,
  event: EventRecord,
  generation: number,
): WorkState {
  if (generation !== state.generation) return state;
  const previous = state.cursors[event.run_id] ?? 0;
  if (event.seq <= previous) return state;
  const next = {
    ...state,
    cursors: { ...state.cursors, [event.run_id]: event.seq },
  };
  const current = state.runs[event.run_id];
  if (
    !current ||
    event.topic_id !== current.topic_id ||
    event.task_revision < current.task_revision
  )
    return next;
  if (
    event.task_revision === current.task_revision &&
    event.execution_version < current.execution_version
  )
    return next;
  if (
    event.scope === "INTERACTIVE" &&
    (event.epoch !== state.epoch || event.topic_id !== state.topic_id)
  )
    return next;
  if (event.seq <= Math.max(current.snapshot_seq, current.event_seq ?? 0))
    return next;
  const value = event.payload.state;
  const updated = {
    ...current,
    task_revision: event.task_revision,
    execution_version: event.execution_version,
    event_seq: event.seq,
    state:
      typeof value === "string" && states.has(value) ? value : current.state,
    stage:
      typeof event.payload.stage === "string"
        ? event.payload.stage
        : current.stage,
    public_plan: Array.isArray(event.payload.plan)
      ? event.payload.plan
      : current.public_plan,
  };
  return { ...next, runs: { ...next.runs, [event.run_id]: updated } };
}
