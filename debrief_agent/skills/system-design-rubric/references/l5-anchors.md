# L5 System Design Behavioral Anchors (Cloud AI Engineer / TSC)

## Core Competencies

### 1. Ambiguity Resolution & Enterprise Framing
- **Positive Signals:**
  - Proactively structures an ambiguous enterprise problem by probing SLA/SLO targets, data residency, multi-tenancy, cost ceilings, and organizational constraints.
  - Connects architectural choices directly to business impact and phased rollout (MVP vs. Day-2 production).
- **Negative Signals:**
  - Relies heavily on the interviewer to define requirements or overlooks enterprise constraints (tenant isolation, governance, latency budgets).
  - Treats the design purely as a greenfield academic exercise disconnected from customer realities.

### 2. End-to-End Architecture & AI/Cloud Trade-off Depth
- **Positive Signals:**
  - Designs a cohesive, production-grade distributed/AI architecture and proactively compares concrete alternatives (e.g., RAG vs. long-context vs. fine-tuning; synchronous vs. event-driven orchestration; hybrid search vs. dense-only).
  - Quantifies bottlenecks with back-of-the-envelope capacity, throughput, token/cost, or latency math.
- **Negative Signals:**
  - Selects buzzword components without justifying trade-offs or evaluating failure modes.
  - Requires repeated hints to identify major architectural flaws or cost explosions.

### 3. Resilience, Scalability & Failure Mode Engineering
- **Positive Signals:**
  - Proactively designs for graceful degradation (circuit breakers, fallback models, dead-letter queues, idempotent consumers, backpressure).
  - Addresses data consistency, cache invalidation, hot-spotting, and multi-region or high-availability failover trade-offs independently.
- **Negative Signals:**
  - Assumes happy-path execution and external API reliability without fallback or retry boundaries.
  - Only considers scaling or failure recovery after direct interviewer prompting.

### 4. Security, Governance & Operational Excellence
- **Positive Signals:**
  - Integrates enterprise security by design: tenant-level IAM/ACL enforcement (including retrieval-time document ACLs), PII redaction, VPC/private networking, audit trails, and prompt-injection/output guardrails.
  - Defines concrete observability and evaluation pipelines (distributed tracing, latency/cost dashboards, offline + online quality evals, canary rollouts).
- **Negative Signals:**
  - Treats security or governance as an afterthought ("we'll just add auth later") or misses data-leakage vectors across tenants.
  - Lacks a concrete plan for monitoring quality drift, hallucinations, or production incidents.
