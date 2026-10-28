import { useEffect, useRef, useState } from "react";
import { ShieldCheck, Clock3, ArrowRight } from "lucide-react";
import type { Review, Metric } from "../api/types";
import { ApiError, errorText } from "../api/client";
import type { RunRecord } from "./state";
import { dateTime } from "../components/Common";
export type DecisionRequest = {
  confirmation_id: string;
  idempotency_key: string;
  token: string;
  decision: Record<string, string>;
};
type Props = {
  run: RunRecord;
  review: Review;
  metrics?: Metric[];
  onDecision: (body: DecisionRequest) => Promise<void>;
  onRefresh: () => void;
};
export function ReviewPanel({
  run,
  review,
  metrics = [],
  onDecision,
  onRefresh,
}: Props) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [now, setNow] = useState(Date.now());
  const pending = useRef<DecisionRequest | null>(null);
  useEffect(() => {
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(clock);
  }, []);
  const valid =
    !stale &&
    run.state === "WAITING_INPUT" &&
    run.run_id === review.run_id &&
    run.task_revision === review.task_revision &&
    new Date(review.expires_at).getTime() > now;
  async function decide() {
    if (!valid || busy || Date.now() >= new Date(review.expires_at).getTime())
      return;
    pending.current ??= {
      confirmation_id: review.confirmation_id,
      idempotency_key: crypto.randomUUID(),
      token: review.token,
      decision:
        review.kind === "query_approval"
          ? { action: "approve" }
          : { action: "clarify", answer: answer.trim() },
    };
    setBusy(true);
    setError("");
    try {
      await onDecision(pending.current);
    } catch (error) {
      setError(errorText(error));
      if (error instanceof ApiError && [401, 403, 409].includes(error.status))
        setStale(true);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="review-panel" aria-label="待确认事项">
      <div className="review-heading">
        <ShieldCheck size={20} />
        <div>
          <strong>
            {review.kind === "clarification"
              ? "先确认分析口径"
              : "查询范围需要你批准"}
          </strong>
          <p>只恢复这条分析任务，其他话题不会受影响。</p>
        </div>
      </div>
      {review.kind === "clarification" ? (
        <>
          <p className="review-question">{review.question}</p>
          <label className="field-label" htmlFor={"answer-" + run.run_id}>
            你的确认
          </label>
          <textarea
            id={"answer-" + run.run_id}
            aria-label="确认口径"
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            rows={3}
            maxLength={2000}
            readOnly={busy || Boolean(error)}
            placeholder="请明确指标、时间范围和筛选条件，也可以说明统计全部数据。"
          />
        </>
      ) : (
        <>
          <p>这次查询预计处理的数据较多。批准后仍受只读权限与执行时限限制。</p>
          <details>
            <summary>查看本次查询</summary>
            <pre>{review.sql}</pre>
            <pre>{JSON.stringify(review.params, null, 2)}</pre>
          </details>
        </>
      )}
      {metrics.length > 0 && (
        <details className="metric-definitions">
          <summary>查看指标口径</summary>
          {metrics.map((m) => (
            <div key={m.metric_id}>
              <strong>{m.name}</strong>
              <p>{m.definition}</p>
            </div>
          ))}
        </details>
      )}
      <div className="review-budget">
        <Clock3 size={14} />
        剩余执行 {review.budget.remaining_seconds} 秒 · 模型已调用{" "}
        {review.budget.model_used} 次 · 查询已执行 {review.budget.query_used} 次
      </div>
      <p className="muted tiny">
        有效至 {dateTime(review.expires_at)}；输入新消息不会批准这项确认。
      </p>
      {!valid && (
        <p className="inline-error" role="alert">
          这项确认已失效，请刷新任务状态。
        </p>
      )}
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      <div className="review-actions">
        <button className="button secondary" onClick={onRefresh}>
          刷新任务
        </button>
        <button
          className="button primary"
          disabled={
            !valid ||
            busy ||
            (review.kind === "clarification" && !answer.trim())
          }
          onClick={() => void decide()}
        >
          {busy
            ? "正在提交"
            : review.kind === "clarification"
              ? "提交口径并继续"
              : "批准本次查询"}
          <ArrowRight size={15} />
        </button>
      </div>
    </section>
  );
}
