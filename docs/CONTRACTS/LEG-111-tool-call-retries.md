# LEG-111 — `policy.retries` retries the tool call (honor Schema 3)

- **Status**: APPROVED (maintainer direction 2026-09-28: retries "se decidió
  ponerlos de manera general").
- **Type**: defect fix (engine) — GitHub issue to be opened by the maintainer.
- **Related**: LEG-013 (approved contract: `policy.retries` = call retries),
  LEG-103 Slice 1 (wrote the contradictory "retries stays 0" clause).

## Problem

LEG-013 (approved) defines Schema 3's tool policy literally as:

> `policy.retries` = how many times the executing entity retries the **call**;
> `policy.timeout` = how long you may wait **per call**.

The implementation contradicts it: `ToolAgent._tool_policy` / `_handle` reject
any non-zero `retries` ("only retries=0 is supported (steps are never retried)").
So a declared retry policy is silently unusable.

Root cause: a conflation. The Session 16 ruling "no lease, no retry, no re-queue
**in the dispatch**" forbids re-queueing a raised step; LEG-103 Slice 1 then
over-applied it to the **tool call**. A bounded retry of the same call is
compatible with polling-only and with "a raised step is never re-queued".

## Contract

1. `policy.retries` is honored: the tool **call** is attempted up to
   `retries + 1` times (a value of `0`/`None` means a single attempt).
2. **Retryable failure** = any exception raised by the tool invocation,
   including a `policy.timeout` expiry. Pre-invocation failures are **not**
   retried: loading the tool, resolving parameters, and the execution-time
   signature check still fail once and surface (rule 9).
3. Retries are **immediate** — no back-off sleep (rule 8: nothing sleeps). Each
   attempt is bounded by the same `policy.timeout`.
4. Every retry is a visible WARNING naming the agent, tool, attempt and cause;
   an exhausted policy surfaces the last error as the step's error result
   (rule 9). The dispatch still never re-queues a step.
5. Policy validation is unchanged: a non-integer / boolean `retries` fails
   loudly; a negative `retries` fails at load (`ToolPolicy`).

## Acceptance

- A tool that fails twice and succeeds on the third call with `retries: 2`
  returns its value; with `retries: 0` the same tool fails once, visibly.
- An exhausted policy surfaces the last error; the number of calls equals
  `retries + 1`.
- A `policy.timeout` expiry is retried like any other call failure.
- The retries-0 clause of LEG-103 Slice 1 is amended (superseded by LEG-013).
- Full gate green.

## Out of scope

- Dispatch re-queue / at-least-once (still removed, rule 8).
- Back-off / delays.
