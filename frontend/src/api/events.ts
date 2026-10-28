import {
  ApiError,
  expireSession,
  sessionGeneration,
  trackController,
} from "./client";
import type { RunEvent } from "./types";
export function watchRun(
  runId: string,
  cursor: () => number,
  onEvent: (event: RunEvent) => void,
  onRefresh: () => void,
) {
  const controller = new AbortController();
  const unregister = trackController(controller);
  const at = sessionGeneration();
  const task = async () => {
    while (!controller.signal.aborted && at === sessionGeneration()) {
      try {
        const response = await fetch(
          `/api/v1/runs/${runId}/events?cursor=${cursor()}`,
          {
            credentials: "same-origin",
            signal: controller.signal,
            headers: { accept: "text/event-stream" },
          },
        );
        if (at !== sessionGeneration()) return;
        if (response.status === 401) {
          expireSession(at);
          return;
        }
        if (response.status === 403 || response.status === 404) {
          onRefresh();
          return;
        }
        if (!response.ok || !response.body)
          throw new ApiError(response.status, "stream_unavailable");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let terminal = false;
        try {
          while (!controller.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            if (buffer.length > 262144) throw new Error("event_too_large");
            const packets = buffer.split(/\r?\n\r?\n/);
            buffer = packets.pop() ?? "";
            for (const packet of packets) {
              if (at !== sessionGeneration()) return;
              const lines = packet.split(/\r?\n/);
              const name = lines
                .find((l) => l.startsWith("event:"))
                ?.slice(6)
                .trim();
              if (name === "auth_expired") {
                expireSession(at);
                return;
              }
              if (name === "access_revoked") {
                onRefresh();
                return;
              }
              if (name !== "run_event") continue;
              const event = JSON.parse(
                lines
                  .filter((l) => l.startsWith("data:"))
                  .map((l) => l.slice(5).trimStart())
                  .join("\n"),
              ) as RunEvent;
              const id = Number(
                lines.find((l) => l.startsWith("id:"))?.slice(3),
              );
              if (
                event.run_id !== runId ||
                !Number.isSafeInteger(event.seq) ||
                event.seq !== id ||
                !["INTERACTIVE", "BACKGROUND"].includes(event.scope) ||
                !Number.isSafeInteger(event.task_revision) ||
                !Number.isSafeInteger(event.execution_version)
              )
                throw new Error("event_invalid");
              onEvent(event);
              onRefresh();
              if (
                [
                  "SUCCEEDED",
                  "FAILED",
                  "CANCELLED",
                  "SUPERSEDED",
                  "TIMED_OUT",
                ].includes(String(event.payload.state))
              )
                terminal = true;
            }
          }
        } finally {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
        if (terminal) return;
      } catch (error) {
        if (controller.signal.aborted || at !== sessionGeneration()) return;
        onRefresh();
      }
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  };
  void task().finally(unregister);
  return () => {
    controller.abort();
    unregister();
  };
}
