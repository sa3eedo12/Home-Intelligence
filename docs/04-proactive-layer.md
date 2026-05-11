# PR 4 — Proactive Layer (Scheduler + Reactive + Policies)

This PR completes the 4-PR Home-Intelligence series by turning the orchestrator into a proactive assistant. Instead of only responding to Telegram prompts, the system now initiates actions from schedules, reacts to home/system streams, and enforces outbound notification policies before Telegram delivery.

## PolicyEngine state machine

All outbound notifications flow through `PolicyEngine.evaluate(payload)` before send.

```text
notify.outbound
   |
   v
[critical?] --yes--> SEND
   |
   no
   v
[allowlist?] (quiet-hours bypass patterns)
   |
   v
[manual mute?] --yes--> SUPPRESS
   |
   no
   v
[rate limit exceeded?] --yes--> SUPPRESS or ROLLUP
   |
   no
   v
[quiet hours active?] --yes--> SUPPRESS (unless allowlisted)
   |
   no
   v
[dedupe seen in window?] --yes--> SUPPRESS
   |
   no
   v
SEND
```

Decision outcomes:
- `send`: notification is forwarded to Telegram.
- `suppress`: message is acknowledged but not sent.
- `rollup`: repeated flood events are aggregated into a summary message.

## Scheduled jobs vs reactive triggers vs manual run

Three proactive pathways now coexist:

1. **Schedules (`schedules.yaml`)**
   - Declarative cron/interval jobs loaded by APScheduler (`Asia/Dubai` timezone).
   - Examples: `morning_brief` at 07:30, `evening_recap` at 21:00, anomaly checks every 15 minutes.

2. **Reactive triggers (`reactive_triggers.yaml`)**
   - Stream-driven rules (`events.home`, `events.system`) with match criteria (`type`, `severity_min`, etc.).
   - Examples: doorbell ring/motion summaries and system threshold breach notifications.

3. **Manual execution (`POST /admin/run-job/{id}`)**
   - Operator-triggered one-off execution of any declared schedule job.
   - Used for recovery/testing without waiting for the next cron tick.

## Fingerprinting and dedupe

Default dedupe fingerprint:

```text
{agent}|{topic}|{text|sha256:64}
```

- `{text|sha256:64}` means SHA-256 of `text`, truncated to 64 hex characters.
- This keeps dedupe stable for semantically identical alerts while avoiding huge Redis keys.

Design guidance for custom fingerprints:
- Include **high-cardinality incident identity** fields (host, metric, entity id) when repeated incidents should remain distinct.
- Exclude volatile fields (timestamps, random IDs) when repeated incidents should collapse.

## `/mute` and `/quiet` interaction

Runtime policy overrides are stored in Redis with TTL:
- `/mute <agent|topic> [minutes]` → `policy:mute:<key>`
- `/quiet on|off|status` controls quiet-hours behavior/visibility

Current precedence (highest first):

```text
critical > allowlist > mute > rate limit > quiet hours > dedupe
```

That ordering ensures true emergencies still pass, while non-critical noise is constrained by operator mute, flood control, and quiet-hour suppression.

## Operating playbook (missing morning brief)

1. Open `/dashboard` and inspect scheduler/notification sections.
2. Confirm `morning_brief` is registered and has a valid next run time.
3. Trigger manual execution: `POST /admin/run-job/morning_brief`.
4. If still missing, inspect recent policy decisions for suppression reasons (`quiet_hours`, `rate_limit`, `dedupe`, `manual_mute`).

## Completion note

This PR finalizes the proactive control plane from the 4-PR rollout:
1. Foundation stack
2. Orchestrator + Telegram + Home Automation
3. Remaining domain agents
4. **Proactive scheduling + reactive triggers + policy enforcement + operator dashboard**
