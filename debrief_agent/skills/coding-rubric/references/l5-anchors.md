# L5 Coding Behavioral Anchors (Cloud AI Engineer / TSC)

## Core Competencies

### 1. Problem Decomposition & Algorithmic Optimization
- **Positive Signals:**
  - Independently frames the problem, identifies hidden constraints (streaming/memory limits, concurrency, rate limits), and compares multiple algorithmic strategies before committing.
  - Proactively optimizes time/space complexity and articulates exact asymptotic and practical performance trade-offs.
- **Negative Signals:**
  - Settles for a suboptimal approach and cannot optimize without step-by-step interviewer guidance.
  - Overlooks practical bottlenecks (e.g., excessive allocations, N+1 calls, lock contention) in production-like scenarios.

### 2. Production-Grade Code Structure & Maintainability
- **Positive Signals:**
  - Designs clean abstractions, cohesive helper functions, clear type annotations, and defensive input validation suitable for production code review.
  - Smoothly adapts the design when requirements change mid-interview without a full rewrite.
- **Negative Signals:**
  - Writes monolithic, tightly coupled code that breaks when an extension or follow-up requirement is introduced.
  - Neglects error handling, resource cleanup, or clear interface boundaries.

### 3. Systematic Testing, Edge Cases & Debugging
- **Positive Signals:**
  - Proactively enumerates tricky edge cases (concurrency/race conditions, malformed payloads, empty/skewed inputs, retries/idempotency) and validates them before being asked.
  - Debugs defects methodically via hypothesis-driven tracing and immediately generalizes the fix.
- **Negative Signals:**
  - Misses critical boundary conditions until prompted by the interviewer.
  - Patches symptoms superficially without verifying whether the invariant holds across inputs.

### 4. Engineering Judgment & Technical Communication
- **Positive Signals:**
  - Communicates intent, invariants, and trade-offs crisply while maintaining strong coding velocity.
  - Connects implementation decisions to real-world maintainability, observability, or customer SDK usability.
- **Negative Signals:**
  - Cannot articulate why a chosen data structure or concurrency model is preferable to alternatives.
  - Requires frequent interviewer intervention to stay structured or recover from dead ends.
