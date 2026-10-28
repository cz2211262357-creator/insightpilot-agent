# Persistent task transitions

| From | To | Trigger |
|---|---|---|
| QUEUED | RUNNING | atomic authorized claim, fresh monotonically increasing execution token |
| RUNNING | QUEUED | explicit transient failure, backoff5s/30s, unchanged active deadline |
| RUNNING | RUNNING | expired lease takeover after5s/30s, token increases, attempts increase |
| RUNNING | WAITING_INPUT | graph checkpoint written first; save remaining active seconds, clear lease,24h wait |
| WAITING_INPUT | QUEUED | internal continuation after bound human approval; preserve counters/time, no automatic-attempt charge |
| RUNNING | SUCCEEDED | final checkpoint already persisted; atomic report+completed message+event with current fence |
| Any active | CANCELLED | owner cancel with expected revision; revision increases |
| Any active | SUPERSEDED | explicit task revision or foreground replacement; old execution/confirmation invalid |
| QUEUED/RUNNING/WAITING_INPUT | TIMED_OUT | queue/absolute execution/confirmation deadline |
| Active | FAILED | revoked identity, exhausted budgets/retries, permanent error |

Terminal states have no outgoing transition. Repeated accepted cancellation returns its existing result. Revisions apply only to active tasks; a delivered report can be followed by a new task. Every immutable run uses a new graph thread. A repeated client_message_id returns its original receipt before touching topic/epoch; different content/scope/topic is409.

Conversation epoch tracks foreground changes and never decreases. Topic activation uses expected_epoch CAS. New messages replace active foreground work; background work keeps its original topic/revision and survives A→B→A. A browser message saying 确认 is an ordinary message, never an approval.

Locks are identity→conversation→run→evidence/confirmation. Checkpoint-role SQL may call the restricted SECURITY DEFINER guard without reading state tables. require_fence requires an outer transaction and locks current authorization and run on the same connection as the write. Expired leases fail even before a new owner exists. The project roles enforce5s idle transaction timeout,2s lock timeout,3s reader statement timeout and5s state/checkpoint statement timeout. No network/model call belongs inside a protected state transaction.

Limits persist before external calls:8model,4query,2repair; foreground120s/background300s active time; queue60s/600s. Recovery does not reset counters or deadlines. Human waiting releases resources and preserves remaining time. This is not a promise of provider exactly-once invocation or provider-side cancellation.
