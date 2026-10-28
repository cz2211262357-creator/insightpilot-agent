import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, it, expect, vi } from "vitest";
import { Composer } from "./Composer";
import { ReviewPanel } from "./ReviewPanel";
import type { RunRecord } from "./state";
import type { Review } from "../api/types";
afterEach(cleanup);
const run: RunRecord = {
  run_id: "run-A",
  topic_id: "A",
  task_revision: 1,
  execution_version: 1,
  scope: "BACKGROUND",
  epoch: 1,
  snapshot_seq: 3,
  state: "WAITING_INPUT",
};
const review: Review = {
  kind: "clarification",
  confirmation_id: "review-A",
  token: "test-bound-capability",
  run_id: "run-A",
  topic_id: "A",
  task_revision: 1,
  question: "请明确销售指标与时间范围 <script>unsafe()</script>",
  budget: {
    model_used: 1,
    query_used: 0,
    repair_used: 0,
    remaining_seconds: "299.000",
  },
  expires_at: new Date(Date.now() + 3600000).toISOString(),
};
it("topic B text consent cannot approve topic A and a mismatched review is disabled", async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  const decision = vi.fn();
  render(
    <>
      <Composer onSend={send} />
      <ReviewPanel
        run={{ ...run, run_id: "run-B", topic_id: "B" }}
        review={review}
        onDecision={decision}
        onRefresh={() => {}}
      />
    </>,
  );
  fireEvent.change(screen.getByLabelText("分析问题"), {
    target: { value: "同意" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送分析" }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  expect(decision).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "提交口径并继续" })).toBeDisabled();
  expect(document.querySelector("script")).toBeNull();
});
it("only the explicit review action resumes the bound task, network retries reuse decision identity", async () => {
  const decision = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue(undefined);
  render(
    <ReviewPanel
      run={run}
      review={review}
      onDecision={decision}
      onRefresh={() => {}}
    />,
  );
  fireEvent.change(screen.getByLabelText("确认口径"), {
    target: { value: "统计全部已支付订单实付销售额" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交口径并继续" }));
  await waitFor(() => expect(screen.getByRole("alert")).toBeVisible());
  fireEvent.click(screen.getByRole("button", { name: "提交口径并继续" }));
  await waitFor(() => expect(decision).toHaveBeenCalledTimes(2));
  expect(decision.mock.calls[0][0]).toEqual(decision.mock.calls[1][0]);
  expect(decision.mock.calls[0][0].confirmation_id).toBe("review-A");
  expect(decision.mock.calls[0][0].decision).toEqual({
    action: "clarify",
    answer: "统计全部已支付订单实付销售额",
  });
});
it("expired confirmation cannot be submitted", () => {
  const decision = vi.fn();
  render(
    <ReviewPanel
      run={run}
      review={{
        ...review,
        expires_at: new Date(Date.now() - 1000).toISOString(),
      }}
      onDecision={decision}
      onRefresh={() => {}}
    />,
  );
  expect(screen.getByRole("button", { name: "提交口径并继续" })).toBeDisabled();
  expect(decision).not.toHaveBeenCalled();
});
