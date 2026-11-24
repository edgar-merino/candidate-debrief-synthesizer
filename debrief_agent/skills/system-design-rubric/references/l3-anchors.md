# L3 System Design Behavioral Anchors (Cloud AI Engineer / TSC)

## Core Competencies

### 1. Requirements Understanding & Basic Framing
- **Positive Signals:**
  - Asks basic clarifying questions about inputs, outputs, and core user flow before sketching components.
  - Restates the problem accurately and focuses on a clean, single-region working design rather than getting lost in premature complexity.
- **Negative Signals:**
  - Jumps straight into naming tools or writing code without understanding what the system needs to do.
  - Misses explicit functional requirements stated in the prompt even after gentle reminders.

### 2. Core Component Design & Data Flow
- **Positive Signals:**
  - Connects standard building blocks (client, API server, database/storage, AI model API) with a clear request/response lifecycle.
  - Explains basic responsibilities of each component and chooses sensible data schemas or API endpoints.
- **Negative Signals:**
  - Leaves major gaps in how data gets from the user to storage or the model and back.
  - Confuses fundamental roles of components (e.g., stateless compute vs. persistent storage, synchronous vs. asynchronous calls).

### 3. Bottleneck Awareness & Guided Trade-offs
- **Positive Signals:**
  - Recognizes basic bottlenecks (e.g., slow external LLM calls, large payloads, database reads) when prompted and suggests reasonable mitigations like caching or background queues.
  - Responds constructively to interviewer hints and adapts the design logically.
- **Negative Signals:**
  - Unable to reason about what happens when traffic increases or an external API fails, even with direct guidance.
  - Treats suggested improvements mechanically without understanding why they help.

### 4. Fundamental Reliability, Security & Debuggability
- **Positive Signals:**
  - Mentions foundational hygiene: API authentication, keeping API keys out of client code, basic input validation, and structured logging for debugging.
  - Considers basic error handling (timeouts, user-friendly fallback messages when a model call fails).
- **Negative Signals:**
  - Hardcodes secrets or ignores authentication/error handling entirely even when asked how the app would be operated safely.
  - Cannot describe how they would debug a failing request in production.
