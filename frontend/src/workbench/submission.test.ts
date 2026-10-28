import { it, expect } from "vitest";
import { SubmissionRegistry, type Submission } from "./submission";
it("keeps complete request context stable across UI changes and a network retry", () => {
  const registry = new SubmissionRegistry();
  const body: Submission = {
    conversation_id: "conversation",
    topic_id: "A",
    expected_epoch: 3,
    client_message_id: "one-message",
    question: "八月销售额",
    scope: "BACKGROUND",
  };
  const original = registry.prepare(body);
  const retry = registry.prepare({
    ...body,
    topic_id: "B",
    expected_epoch: 8,
    scope: "INTERACTIVE",
  });
  expect(retry).toEqual(original);
  expect(retry.scope).toBe("BACKGROUND");
  expect(retry.topic_id).toBe("A");
  expect(retry.expected_epoch).toBe(3);
});
