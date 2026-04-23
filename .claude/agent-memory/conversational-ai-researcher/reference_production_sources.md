---
name: Canonical voice-AI and queue sources
description: Verified primary sources for scheduler, queue, webhook, and provider-abstraction patterns used when advising on voice-AI infra.
type: reference
---

**Postgres-as-queue with SKIP LOCKED**
- https://leontrolski.github.io/postgres-as-queue.html — canonical pattern writeup
- https://www.crunchydata.com/blog/message-queuing-using-native-postgresql — Crunchy/2ndQuadrant-style guide
- Oban (Elixir) and River (Go) are production implementations of the same pattern

**Webhook contracts**
- Twilio StatusCallback: https://www.twilio.com/docs/voice/twiml#callbacks-and-statuscallbacks
- Twilio webhook connection/retry: https://www.twilio.com/docs/usage/webhooks/webhooks-connection-overrides
- Retell webhook: https://docs.retellai.com/features/webhook
- Stripe idempotency keys: https://stripe.com/docs/api/idempotent_requests

**Provider abstraction reference implementations**
- Pipecat transports: https://github.com/pipecat-ai/pipecat/tree/main/src/pipecat/transports
- LiveKit Agents: https://github.com/livekit/agents/tree/main/livekit-agents

**Scheduler papers**
- Shreedhar & Varghese, DRR, SIGCOMM '95: https://dl.acm.org/doi/10.1145/248156.248166 (O(1) amortized per-packet)
- At small flow counts (<~20), weighted RR suffices — DRR's win is at thousands of flows.

**Observability UX reference**
- Inngest function-run timeline: https://www.inngest.com/docs/platform/monitor/function-logs — good model for an audit-log-as-visualization UX.
