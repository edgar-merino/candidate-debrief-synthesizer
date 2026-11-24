# L4 System Design Behavioral Anchors (Cloud AI Engineer / TSC)

## Core Competencies

### 1. Requirements Gathering & Problem Framing
- **Positive Signals:**
  - Clarifies core functional requirements, expected user volume, and basic latency/availability goals before drawing components.
  - Identifies key constraints (e.g., data freshness, budget, or basic compliance boundaries) and scopes a realistic MVP.
- **Negative Signals:**
  - Jumps immediately into diagramming specific products without confirming use cases or scale.
  - Ignores stated constraints or over-engineers for unrealistic global scale when a simpler design fits.

### 2. Core Architecture & Component Selection
- **Positive Signals:**
  - Proposes a coherent end-to-end architecture with clear data flow between ingestion, processing, storage, and serving layers.
  - Selects appropriate cloud/AI building blocks (e.g., managed queues, relational vs. NoSQL, vector store vs. search) with sound reasoning.
- **Negative Signals:**
  - Treats critical components as "magic black boxes" without explaining how data moves between them.
  - Chooses mismatched technologies (e.g., unindexed relational table for high-dimensional vector search) and cannot explain why.

### 3. Scalability, Reliability & Trade-off Reasoning
- **Positive Signals:**
  - Identifies obvious single points of failure and proposes standard mitigations (retries with backoff, caching, horizontal scaling).
  - Articulates at least one meaningful trade-off (e.g., consistency vs. latency, managed service vs. custom control) when prompted.
- **Negative Signals:**
  - Cannot explain what breaks first under 10x traffic even with interviewer prompting.
  - Presents a single design as strictly optimal without acknowledging downsides.

### 4. Security, Observability & Operational Readiness
- **Positive Signals:**
  - Mentions basic authentication/authorization (IAM, least privilege), secrets management, and encryption in transit/at rest.
  - Identifies core health signals to monitor (latency, error rate, model/API quota exhaustion) and basic alerting.
- **Negative Signals:**
  - Leaves public endpoints unsecured or embeds credentials in application config.
  - Omits logging, monitoring, or failure recovery considerations entirely.
