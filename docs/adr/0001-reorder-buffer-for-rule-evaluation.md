# 1. Reorder buffer for rule evaluation

Status: accepted

## Context

Rule state is keyed by reading time, and each rule skips readings at or before the last one it saw (`lastTs`), because a standard SQS queue reorders and duplicates messages. That keeps a redelivery from re-opening or closing an alert, but it also drops any batch that reaches the rules after a later second of the same engine.

The chaos run in [RESULTS.md](../../RESULTS.md) hit exactly that on `kind`. A processor pod was hard-killed after storing a batch's readings but before recording its alerts. SQS redelivered the message after the 30 s visibility timeout. Meanwhile other pods had processed later seconds of the same engine, so the rules skipped the late batch's readings, and an anomaly confined to that batch never raised an alert (20 alerts expected, 18 raised). A delayed batch can also look like missing seconds to the dropout rule.

## Decision

The rules run per flight and engine strictly in batch-second order, through a reorder buffer in Valkey.

- Readings are still stored at once, idempotently.
- The processor puts each message's batch into its engine's buffer (a hash of second to batch), then deletes the SQS message. From then on the batch lives only in Valkey, the same durability as the rule state that is already there: losing Valkey loses both.
- A drain runs the rules on buffered batches in order, from the engine's next expected second. A flight's first expected second is its start, which the `flightId` encodes. The drain stops at a missing second.
- A missing second is given up once the earliest later batch has been buffered for `reorderWaitSeconds` (sensors.yaml, 40 s: the 30 s visibility timeout plus margin to receive and store the redelivery). The rules then see it as absent, exactly like a batch that was never sent, and draining continues. A batch that arrives after that is stored but dropped from the rules.
- A drain of every buffered engine runs on every poll-loop iteration, after the received messages are buffered and before the next receive, so a tail behind a gap drains even when no new message arrives (a long poll takes at most 20 s).
- One processor drains an engine at a time, under a Valkey lock with a 10 s TTL renewed before each batch. The others only buffer. The engine's cursor is its next expected second and never moves back. A batch before the cursor is dropped, so a redelivery is never run twice.
- The `lastTs` skip stays as a safety net. With the buffer it drops nothing in normal operation.

## Consequences

- Batches that arrive in order reach the rules in the same poll that received them.
- After a crash or a lost batch, detection for that engine waits up to `reorderWaitSeconds` (plus up to one 20 s long poll), and up to the 10 s lock TTL if the drainer itself died.
- A batch that never arrives costs that wait once, then is treated as absent.
- A batch whose recording still fails once it has been buffered for `reorderWaitSeconds` goes to the DLQ, and the drain treats it as absent and continues.
- The one-drainer-per-engine guarantee holds only while recording one batch takes less than the 10 s lock TTL. A slower drainer can overlap the next one. The idempotent rules absorb that (a critical email may go out twice), and the cursor never moves back.
- Valkey now also holds up to `reorderWaitSeconds` of batches per engine while a second is missing.

## Alternatives considered

- Only graceful kills in the chaos run. This hides a real failure mode instead of fixing it.
- Document the limit. This leaves an anomaly in a redelivered batch undetected.
- SQS FIFO queue. It is ordered per message group, but it was rejected because it hides the idempotency story and caps throughput.
