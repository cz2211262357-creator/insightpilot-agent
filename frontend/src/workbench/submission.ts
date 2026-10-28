export type Submission = {
  conversation_id: string;
  topic_id: string;
  expected_epoch: number;
  client_message_id: string;
  question: string;
  scope: "BACKGROUND" | "INTERACTIVE";
};
export class SubmissionRegistry {
  private frames = new Map<string, Readonly<Submission>>();
  prepare(body: Submission): Readonly<Submission> {
    const existing = this.frames.get(body.client_message_id);
    if (existing) return existing;
    const captured = Object.freeze({ ...body });
    this.frames.set(body.client_message_id, captured);
    return captured;
  }
  complete(id: string) {
    this.frames.delete(id);
  }
}
